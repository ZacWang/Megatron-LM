# Copyright (c) 2022, NVIDIA CORPORATION. All rights reserved.

from apex.optimizers import FusedAdam as Adam
from apex.optimizers import FusedSGD as SGD
from collections import defaultdict

from megatron import get_args

from .distrib_optimizer import DistributedOptimizer
from .grad_scaler import ConstantGradScaler, DynamicGradScaler
from .optimizer import Float16OptimizerWithFloat16Params, FP32Optimizer

def get_param_groups(modules,
                     no_weight_decay_cond,
                     scale_lr_cond,
                     lr_mult):
    """creates param groups based on weight decay condition (regularized vs non regularized)
       and learning rate scale condition (args.lr vs lr_mult * args.lr)
       scale_lr_cond is used during finetuning where head of the network requires a scaled
       version of the base learning rate.
    """
    wd_no_scale_lr = []
    wd_scale_lr = []
    no_wd_no_scale_lr = []
    no_wd_scale_lr = []
    for module in modules:
        for name, param in module.named_parameters():
            if not param.requires_grad:
                continue

            if no_weight_decay_cond is not None:
                no_wd = no_weight_decay_cond(name, param)
            else:
                # do not regularize biases nor Norm parameters
                no_wd = name.endswith(".bias") or len(param.shape) == 1

            if scale_lr_cond is not None:
                scale_lr = scale_lr_cond(name, param)
            else:
                scale_lr = False

            if not no_wd and not scale_lr:
                wd_no_scale_lr.append(param)
            elif not no_wd and scale_lr:
                wd_scale_lr.append(param)
            elif no_wd and not scale_lr:
                no_wd_no_scale_lr.append(param)
            else:
                no_wd_scale_lr.append(param)

    param_groups = []
    if len(wd_no_scale_lr):
        param_groups.append({'params': wd_no_scale_lr, 'wd_mult': 1.0, 'lr_mult': 1.0})
    if len(wd_scale_lr):
        param_groups.append({'params': wd_scale_lr, 'wd_mult': 1.0, 'lr_mult': lr_mult})
    if len(no_wd_no_scale_lr):
        param_groups.append({'params': no_wd_no_scale_lr, 'wd_mult': 0.0, 'lr_mult': 1.0})
    if len(no_wd_scale_lr):
        param_groups.append({'params': no_wd_scale_lr, 'wd_mult': 0.0, 'lr_mult': lr_mult})

    return param_groups


def set_param_groups_defaults(params, **kwargs):
    param_groups = list(params)
    if not isinstance(param_groups[0], dict):
        param_groups = [{'params': param_groups}]
    for param_group in param_groups:
        if 'max_lr' not in param_group:
            param_group['max_lr'] = kwargs['lr']
        if 'weight_decay' not in param_group:
            param_group['weight_decay'] = kwargs.get('weight_decay', 0.0)
        if 'min_lr' not in param_group:
            param_group['min_lr'] = kwargs['min_lr']
    return param_groups


def get_mup_param_groups(params, decoupled_wd=True, **kwargs):
    new_param_groups = []
    for param_group in set_param_groups_defaults(params, **kwargs):
        # For every existing param group, we split into several new groups
        def new_group():
            new_g = {k: v for k, v in param_group.items() if k != 'params'}
            new_g['params'] = []
            return new_g

        # The matrix-like weights might need multiple groups since weights
        # might have different width multipliers. We use width_mult as the key.
        matrix_like_p = defaultdict(new_group)
        input_layer_p = new_group()
        output_layer_p = new_group()
        vector_like_p = new_group()
        for p in param_group['params']:
            assert hasattr(p, 'infshape'), (
                f'A parameter with shape {p.shape} does not have `infshape` attribute. '
                'Did you forget to call `mup.set_base_shapes` on the model?'
            )
            if p.infshape.ninf() == 2:
                matrix_like_p[p.infshape.width_mult()]['params'].append(p)
            elif p.infshape.ninf() > 2:
                raise NotImplementedError('more than 2 inf dimensions')
            else:
                if 'embed' in p.var_name and kwargs['input_lr'] > 0:
                    # Set a different learning rate for the input layer.
                    input_layer_p['params'].append(p)
                    input_layer_p['max_lr'] = kwargs['input_lr']
                elif 'output' in p.var_name and kwargs['output_lr'] > 0:
                    # Set a different learning rate for the output layer.
                    output_layer_p['params'].append(p)
                    output_layer_p['max_lr'] = kwargs['output_lr']
                else:
                    vector_like_p['params'].append(p)

        for width_mult, group in matrix_like_p.items():
            # Scale the max learning rate and weight decay accordingly.
            # We keep min_lr unchanged, to achieve a better control of final LR.
            group['max_lr'] /= width_mult
            assert group['max_lr'] >= group['min_lr'], \
                f'Group with width multiplier {width_mult} has a smaller max_lr than min_lr.'
            if not decoupled_wd:
                group['weight_decay'] *= width_mult
        # Only append non-empty groups.
        new_param_groups.extend([v for v in matrix_like_p.values() if v['params']])
        if input_layer_p['params']:
            new_param_groups.append(input_layer_p)
        if output_layer_p['params']:
            new_param_groups.append(output_layer_p)
        if vector_like_p['params']:
            new_param_groups.append(vector_like_p)
    return new_param_groups


def get_megatron_optimizer(model,
                           no_weight_decay_cond=None,
                           scale_lr_cond=None,
                           lr_mult=1.0):
    args = get_args()

    # Base optimizer.
    param_groups = get_param_groups(model,
                                    no_weight_decay_cond,
                                    scale_lr_cond,
                                    lr_mult)
    if args.use_mup:
        param_groups = get_mup_param_groups(
                        param_groups,
                        lr=args.lr,
                        min_lr=args.min_lr,
                        weight_decay=args.weight_decay,
                        input_lr=args.input_lr,
                        output_lr=args.output_lr)

    if args.optimizer == 'adam':
        optimizer = Adam(param_groups,
                         lr=args.lr,
                         weight_decay=args.weight_decay,
                         betas=(args.adam_beta1, args.adam_beta2),
                         eps=args.adam_eps)
    elif args.optimizer == 'sgd':
        optimizer = SGD(param_groups,
                        lr=args.lr,
                        weight_decay=args.weight_decay,
                        momentum=args.sgd_momentum)
    else:
        raise Exception('{} optimizer is not supported.'.format(
            args.optimizer))

    # Determine whether the params have main-grad field.
    params_have_main_grad = True

    # Mixed precision optimizer.
    # - Note: both the Float16Optimizer and the DistributedOptimizer inherit
    #   from the MixedPrecisionOptimizer, which manages any optimizer where
    #   the model params and main params are distinct.
    if args.fp16 or args.bf16 or args.use_distributed_optimizer:

        # Grad scaler:
        #    if loss-scale is provided, instantiate the constant scaler.
        #    if we are using fp16 and loss-scale is not present, use a
        #       dynamic scaler.
        #    otherwise we are running in bf16 with no loss-scale so
        #       leave it as None.
        grad_scaler = None

        # Constant loss scale.
        if args.loss_scale:
            grad_scaler = ConstantGradScaler(args.loss_scale)

        # Dynamic loss scale.
        else:
            if args.fp16:
                grad_scaler = DynamicGradScaler(
                    initial_scale=args.initial_loss_scale,
                    min_scale=args.min_loss_scale,
                    growth_factor=2.0,
                    backoff_factor=0.5,
                    growth_interval=args.loss_scale_window,
                    hysteresis=args.hysteresis)

        # Megatron optimizer.
        opt_ty = DistributedOptimizer \
            if args.use_distributed_optimizer else \
            Float16OptimizerWithFloat16Params
        return opt_ty(optimizer,
                      args.clip_grad,
                      args.log_num_zeros_in_grad,
                      args.check_for_nan_in_loss_and_grad,
                      params_have_main_grad,
                      args.fp16,
                      args.bf16,
                      args.params_dtype,
                      grad_scaler,
                      model)

    # FP32.
    return FP32Optimizer(optimizer, args.clip_grad,
                         args.log_num_zeros_in_grad,
                         args.check_for_nan_in_loss_and_grad,
                         params_have_main_grad,
                         model)
