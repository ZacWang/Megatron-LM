# coding=utf-8
# Copyright (c) 2022, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Get paths of data batches for training, adding."""

import glob
import h5py
import os
import torch

def get_all_data_paths(args):

    # Get data paths.
    paths = glob.glob(os.path.join(args.data_dir, "*.hdf5"))
    paths.sort()

    # Count & print vecs.
    if True:
        rank = torch.distributed.get_rank()
        n = 0
        for i, p in enumerate(paths):
            if rank == 0 and i % 50 == 0:
                print(
                    "counting feat path %d / %d." % (i, len(paths)),
                    flush = True,
                )
            f = h5py.File(p, "r")
            # if 1:
            n += len(f["data"])
            # else:
            #     # from lutil import pax
            #     # pax({"p": p})
            #     try:
            #         n += len(f["feats"]) # boxin
            #     except Exception as e:
            #         pass
            f.close()
        if rank == 0:
            print("total vecs: %d." % n)

    return paths

def get_train_add_data_paths(args):

    # Get all available data paths.
    all_paths = get_all_data_paths(args)

    # Filter train, add subsets.
    ntrain = None; train_paths = None
    nadd = None; add_paths = None
    ntotal = 0
    for path_index, path in enumerate(all_paths):
        f = h5py.File(path, "r")
        n = len(f["data"])
        f.close()

        ntotal += n

        if ntotal >= args.ntrain and ntrain is None:
            ntrain = ntotal
            train_paths = list(all_paths[:(path_index+1)])
        if ntotal >= args.nadd and nadd is None:
            nadd = ntotal
            add_paths = list(all_paths[:(path_index+1)])

        if ntrain is not None and nadd is not None:
            break

    # Error if not enough data.
    if ntrain is None or nadd is None:
        raise Exception("insufficient data paths?")

    return ntrain, nadd, train_paths, add_paths
