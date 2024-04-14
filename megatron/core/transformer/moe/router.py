# Copyright (c) 2023, NVIDIA CORPORATION. All rights reserved.

import math
from abc import ABC, abstractmethod
from typing import Callable, List

import torch

from megatron.core import parallel_state, tensor_parallel
from megatron.core.parallel_state import get_tensor_and_expert_parallel_group
from megatron.core.tensor_parallel import get_cuda_rng_tracker, get_data_parallel_rng_tracker_name
from megatron.core.tensor_parallel.random import (
    get_cuda_rng_tracker,
    get_data_parallel_rng_tracker_name,
)
from megatron.core.transformer.module import MegatronModule
from megatron.core.transformer.moe.moe_utils import (
    MoEAuxLossAutoScaler,
    sinkhorn,
    switch_load_balancing_loss_func,
    z_loss_func,
)
from megatron.core.transformer.transformer_config import TransformerConfig
from megatron.core.transformer.utils import attention_mask_func


class Router(ABC, MegatronModule):
    """Base Router class"""

    def __init__(self, config: TransformerConfig) -> None:
        """
        Initialize the Router module.

        Args:
            config (TransformerConfig): Configuration object for the Transformer model.
        """
        super().__init__(config)
        self.config = config
        self.num_experts = self.config.num_moe_experts
        self.moe_aux_loss_func = None

        # Initialize the gate weights.
        self.weight = torch.nn.Parameter(
            torch.empty((self.config.num_moe_experts, self.config.hidden_size))
        )
        if config.moe_groupedmoe:
            # use different router on different TP ranks
            config.init_method(self.weight)
            # FIXME expert parallel not considered
            setattr(self.weight, 'allreduce', True)
        else:
            with get_cuda_rng_tracker().fork(get_data_parallel_rng_tracker_name()):
                config.init_method(self.weight)
            setattr(self.weight, 'sequence_parallel', config.sequence_parallel)

    def gating(self, input: torch.Tensor):
        """Forward pass of the router gate.

        Args:
            input (torch.Tensor): Input tensor.

        Returns:
            torch.Tensor: Logits tensor.
        """
        logits = torch.nn.functional.linear(input, self.weight)
        return logits

    @abstractmethod
    def routing(self, logits: torch.Tensor):
        """Routing function.

        Args:
            logits (torch.Tensor): Logits tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Tuple of tensors representing max probs and the indices.
        """
        raise NotImplementedError("Routing function not implemented.")

    def forward(self, input: torch.Tensor):
        """
        Forward pass of the router.

        Args:
            input (torch.Tensor): Input tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: scores and indices.
        """
        self.hidden = input.shape[-1]

        logits = self.gating(input)
        logits = logits.view(-1, self.config.num_moe_experts)

        scores, indices = self.routing(logits)

        return scores, indices


class TopKRouter(Router):
    """Route each token to the top-k experts."""

    def __init__(self, config: TransformerConfig,) -> None:
        """Initialize the zero token dropping router.

        Args:
            config (TransformerConfig): The configuration for the transformer model.
        """
        super().__init__(config=config)
        assert config.moe_token_dropping is False
        self.topk = self.config.moe_router_topk
        self.routing_type = self.config.moe_router_load_balancing_type
        self.moe_aux_loss_func = switch_load_balancing_loss_func
        self.input_jitter = None

    def sinkhorn_load_balancing(self, logits: torch.Tensor):
        """Apply sinkhorn routing to the logits tensor.

        Args:
            logits (torch.Tensor): The logits tensor.

        Returns:
            torch.Tensor: The logits tensor after applying sinkhorn routing.
        """

        def _sinkhorn_activation(logits):
            if self.topk == 1:
                logits = torch.sigmoid(logits)
            else:  # k > 1
                logits = torch.softmax(logits, dim=-1, dtype=torch.float32).type_as(logits)
            return logits

        assert self.config.moe_aux_loss_coeff == 0, "Sinkhorn routing does not support aux loss."
        if self.training:
            with torch.no_grad():
                norm_logits = sinkhorn(
                    logits.to(dtype=torch.float32)
                )  # explicit fp32 conversion for stability
                _, indices = torch.topk(norm_logits, k=self.topk, dim=1)
            logits = _sinkhorn_activation(logits)
            scores = torch.gather(logits, 1, indices)
        else:
            logits = _sinkhorn_activation(logits)
            scores, indices = torch.topk(logits, k=self.topk, dim=1)
        return scores, indices
    
    def sigmoid_load_balancing(self, logits: torch.Tensor):
        """Apply loss-based load balancing to the logits tensor.

        Args:
            logits (torch.Tensor): The logits tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: The scores and the indices tensor after applying load balancing.
        """
        if self.config.moe_dropout and self.training:
            mask = torch.empty_like(logits).bernoulli_(self.config.moe_dropout).bool()
            logits = attention_mask_func(logits, mask)

        probs = torch.sigmoid(logits)
        scores, indices = torch.topk(probs, k=self.topk, dim=1)

        # p = torch.softmax(logits, dim=-1, dtype=torch.float32).mean(0)
        # aux_loss = self.config.moe_aux_loss_coeff * p @ torch.log(p)
        # scores = MoEAuxLossAutoScaler.apply(scores, aux_loss)

        
        scores = self.apply_aux_loss(self.moe_aux_loss_func, torch.softmax(logits, dim=-1, dtype=torch.float32), indices, activation=scores)

        return scores, indices

    def aux_loss_load_balancing(self, logits: torch.Tensor):
        """Apply loss-based load balancing to the logits tensor.

        Args:
            logits (torch.Tensor): The logits tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: The scores and the indices tensor after applying load balancing.
        """
        match self.config.moe_router_type:
            case 'mixtral':
                top_logits, indices = torch.topk(logits, k=self.topk, dim=1)
                scores = torch.softmax(top_logits, dim=-1, dtype=torch.float32).type_as(logits)
                probs = torch.softmax(logits, dim=-1, dtype=torch.float32)
            case 'st':
                probs = torch.softmax(logits, dim=-1, dtype=torch.float32).type_as(logits)
                scores, indices = torch.topk(probs, k=self.topk, dim=1)
            case 'grouped':
                probs, scores, indices = grouped_router(logits, num_moe_experts=self.config.num_moe_experts, topk=self.topk, moe_group_size=self.config.moe_group_size)
            case _:
                raise NotImplementedError
        # Apply load balancing loss
        scores = self.apply_aux_loss(self.moe_aux_loss_func, probs, indices, activation=scores)
        return scores, indices
    
    def btx_aux_loss_load_balancing(self, logits: torch.Tensor):
        """Apply loss-based load balancing to the logits tensor.
        Calculate the auxiliary loss for better load balacing. 
        Please refer to the BTX paper (https://arxiv.org/abs/2403.07816) for details.

        Args:
            logits (torch.Tensor): The logits tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: The scores and the indices tensor after applying load balancing.
        """
        top_logits, indices = torch.topk(logits, k=self.topk, dim=1)
        scores32 = torch.softmax(top_logits, dim=-1, dtype=torch.float32)
        # Apply load balancing loss
        probs = torch.softmax(logits, dim=-1, dtype=torch.float32)
        
        if self.config.moe_aux_loss_type == 'btx_nograd':
            with torch.no_grad():
                top_logits_scattered = torch.zeros_like(probs)
                top_logits_scattered.scatter_(-1, indices, scores32)
        else:
            top_logits_scattered = torch.zeros_like(probs)
            top_logits_scattered.scatter_(-1, indices, scores32)
        aux_loss = self.config.moe_aux_loss_coeff * self.config.num_moe_experts * top_logits_scattered.mean(0) @ probs.mean(0)
        
        scores = scores32.type_as(logits)
        scores = MoEAuxLossAutoScaler.apply(scores, aux_loss)

        return scores, indices

    def apply_aux_loss(
        self,
        loss_func: Callable,
        probs: torch.Tensor,
        indices: torch.Tensor,
        activation: torch.Tensor,
    ):
        """Applies auxiliary loss to the MoE layer.

        Args:
            loss_func (callable): The loss function to be used.
            probs (torch.Tensor): The probabilities output by the MoE layer.
            indices (torch.Tensor): The indices of the selected experts.
            activation (torch.Tensor): The activation tensor to attach the gradient function to.

        Returns:
            torch.Tensor: The activation tensor with the attached gradient function.
        """
        mask = torch.nn.functional.one_hot(indices, num_classes=self.num_experts).sum(dim=1)
        aux_loss = loss_func(probs, mask, self.config.moe_aux_loss_coeff)
        activation = MoEAuxLossAutoScaler.apply(activation, aux_loss)
        return activation

    def apply_z_loss(self, logits):
        """Encourages the router's logits to remain small to enhance stability.
        Please refer to the ST-MoE paper (https://arxiv.org/pdf/2202.08906.pdf) for details.
        
        Args:
            logits (torch.Tensor): The logits of the router.
        
        Returns:
            torch.Tensor: The logits after applying the z-loss.
        """
        if self.config.moe_z_loss_coeff is not None:
            z_loss = z_loss_func(logits, self.config.moe_z_loss_coeff)
            logits = MoEAuxLossAutoScaler.apply(logits, z_loss)
        return logits

    def apply_input_jitter(self, input: torch.Tensor):
        """Add noise to the input tensor.
        Refer to https://arxiv.org/abs/2101.03961.

        Args:
            input (Tensor): Input tensor.

        Returns:
            Tensor: Jittered input.
        """
        if self.config.moe_input_jitter_eps is not None:
            eps = self.config.moe_input_jitter_eps
            if self.input_jitter is None:
                self.input_jitter = torch.distributions.uniform.Uniform(
                    torch.tensor(1.0 - eps, device=input.device),
                    torch.tensor(1.0 + eps, device=input.device),
                ).rsample
            return input * self.input_jitter(input.shape)
        else:
            return input

    def routing(self, logits: torch.Tensor):
        """Top-k routing function

        Args:
            logits (torch.Tensor): Logits tensor.

        Returns:
            Tuple[torch.Tensor, torch.Tensor]: Probs and the indices tensor.
        """
        logits = logits.view(-1, self.config.num_moe_experts)

        # Apply Z-Loss
        logits = self.apply_z_loss(logits)
        # Apply input jitter
        logits = self.apply_input_jitter(logits)

        if self.routing_type == "sinkhorn":
            scores, indices = self.sinkhorn_load_balancing(logits)
        elif self.routing_type == "aux_loss":
            if self.config.moe_aux_loss_type == 'switch':
                scores, indices = self.aux_loss_load_balancing(logits)
            elif 'btx' in self.config.moe_aux_loss_type:
                scores, indices = self.btx_aux_loss_load_balancing(logits)
            else:
                raise ValueError(f"Unsupported aux loss type: { self.config.moe_aux_loss_type}")
        elif self.routing_type == "none":
            # A naive top-k routing without load balancing
            top_logits, indices = torch.topk(logits, k=self.topk, dim=1)
            scores = torch.softmax(top_logits, dim=-1, dtype=torch.float32).type_as(logits)
        elif self.routing_type == "sigmoid":
            scores, indices = self.sigmoid_load_balancing(logits)
        else:
            raise ValueError(f"Unsupported MoE routing type: {self.routing_type}")

        return scores, indices

def grouped_router(logits, num_moe_experts=64, topk=16, moe_group_size=8):
    """put MoE in groups like tensor parallel. 
    e.g. 8 experts, top4, 2 groups
    compute top2 within every 4 experts group

    The computational complexity of the follow two should be equivalent
    - num_moe_experts=64, topk=16, moe_group_size=8, ffn_size = 128
    - num_moe_experts=8, topk=2, moe_group_size=1, ffn_size = 1024

    Args:
        logits (Tensor): (#tokens, #experts)
    
    In [6]: logits = torch.randn(4, 8)

    In [7]: num_moe_experts=8

    In [8]: topk=4

    In [9]: moe_group_size=2

    In [10]: probs, scores, adjusted_indices = grouped_router(logits, num_moe_experts, topk, moe_group_size)

    In [11]: logits
    Out[11]:
    tensor([[ 0.6605,  0.5067, -2.7087, -1.0703, -0.4339, -0.0618,  0.3023, -0.6805],
            [ 0.2585,  1.1013,  1.2551,  0.0448,  0.8753, -0.0532,  1.7316,  1.0494],
            [ 0.3005, -1.2615, -0.4880, -0.7913,  0.1651,  1.3444,  0.3161, -0.6222],
            [-1.4876, -0.2928,  0.2573,  0.9561, -0.3219,  0.8441, -0.6582, -0.6504]])

    In [12]: scores
    Out[12]:
    tensor([[0.2597, 0.2227, 0.1816, 0.1261],
            [0.1694, 0.1452, 0.2728, 0.1379],
            [0.1403, 0.0638, 0.3985, 0.1425],
            [0.2904, 0.1444, 0.2597, 0.0809]])

    In [13]: adjusted_indices
    Out[13]:
    tensor([[0, 1, 6, 5],
            [2, 1, 6, 7],
            [0, 2, 5, 6],
            [3, 2, 5, 4]])
    """
    probs = torch.softmax(logits, dim=-1, dtype=torch.float32)
    
    group_num_experts = num_moe_experts // moe_group_size
    group_topk = topk // moe_group_size
    # (#tokens, #groups, #experts)
    scores, indices = torch.topk(probs.view(-1, moe_group_size, group_num_experts), k=group_topk, dim=-1)
    # Reshape the scores back
    scores = scores.view(-1, topk)

    # Adjust the indices to correspond to the original tensor
    adjusted_indices = indices + torch.arange(0, num_moe_experts, group_num_experts, device=indices.device).view(1, -1, 1)
    adjusted_indices = adjusted_indices.view(-1, topk)
    return probs, scores, adjusted_indices
    
