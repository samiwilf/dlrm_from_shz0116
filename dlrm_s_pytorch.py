# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.
#
# Description: an implementation of a deep learning recommendation model (DLRM)
# The model input consists of dense and sparse features. The former is a vector
# of floating point values. The latter is a list of sparse indices into
# embedding tables, which consist of vectors of floating point values.
# The selected vectors are passed to mlp networks denoted by triangles,
# in some cases the vectors are interacted through operators (Ops).
#
# output:
#                         vector of values
# model:                        |
#                              /\
#                             /__\
#                               |
#       _____________________> Op  <___________________
#     /                         |                      \
#    /\                        /\                      /\
#   /__\                      /__\           ...      /__\
#    |                          |                       |
#    |                         Op                      Op
#    |                    ____/__\_____           ____/__\____
#    |                   |_Emb_|____|__|    ...  |_Emb_|__|___|
# input:
# [ dense features ]     [sparse indices] , ..., [sparse indices]
#
# More precise definition of model layers:
# 1) fully connected layers of an mlp
# z = f(y)
# y = Wx + b
#
# 2) embedding lookup (for a list of sparse indices p=[p1,...,pk])
# z = Op(e1,...,ek)
# obtain vectors e1=E[:,p1], ..., ek=E[:,pk]
#
# 3) Operator Op can be one of the following
# Sum(e1,...,ek) = e1 + ... + ek
# Dot(e1,...,ek) = [e1'e1, ..., e1'ek, ..., ek'e1, ..., ek'ek]
# Cat(e1,...,ek) = [e1', ..., ek']'
# where ' denotes transpose operation
#
# References:
# [1] Maxim Naumov, Dheevatsa Mudigere, Hao-Jun Michael Shi, Jianyu Huang,
# Narayanan Sundaram, Jongsoo Park, Xiaodong Wang, Udit Gupta, Carole-Jean Wu,
# Alisson G. Azzolini, Dmytro Dzhulgakov, Andrey Mallevich, Ilia Cherniavskii,
# Yinghai Lu, Raghuraman Krishnamoorthi, Ansha Yu, Volodymyr Kondratenko,
# Stephanie Pereira, Xianjie Chen, Wenlin Chen, Vijay Rao, Bill Jia, Liang Xiong,
# Misha Smelyanskiy, "Deep Learning Recommendation Model for Personalization and
# Recommendation Systems", CoRR, arXiv:1906.00091, 2019

#TERMS:
#
# ln_
# qr_       quotient-remainder trick
# md_       mixed-dimension trick
# lS_       layer sparse?
# ls_i      Indices. list (type tensor) of np.argmax-type values representing which rows to select.   
#           Example and explanation: list_of_embedding_vectors[ ls_i[0] ] is how 
#           ls_i[foo] is used as a key-value lookup to retrieve an embedding vec.
# ls_o      Offsets. list (type tensor) of offsets that determine which selected embeddings are grouped
#           together for the 'mode' operation. (Mode operation examples: sum, mean, max)


from __future__ import absolute_import, division, print_function, unicode_literals

# miscellaneous
import builtins
import functools
# import bisect
# import shutil
import time
import json
# data generation

import torch

import dlrm_data_pytorch as dp

# numpy
import numpy as np
import socket

# onnx
# The onnx import causes deprecation warnings every time workers
# are spawned during testing. So, we filter out those warnings.
import warnings
with warnings.catch_warnings():
    warnings.filterwarnings("ignore", category=DeprecationWarning)
## import onnx

# pytorch
import torch
from torch import onnx
import torch.nn as nn
from torch._ops import ops
from torch.nn.parallel.parallel_apply import parallel_apply
from torch.nn.parallel.replicate import replicate
from torch.nn.parallel.scatter_gather import gather, scatter

# For distributed run
import extend_distributed as ext_dist

# quotient-remainder trick
from tricks.qr_embedding_bag import QREmbeddingBag
# mixed-dimension trick
from tricks.md_embedding_bag import PrEmbeddingBag, md_solver

import sklearn.metrics

import uuid
import project
from torch.nn.parallel import DistributedDataParallel as DDP

import dlrm_data as dd

# import synthetic_data_loader as fb_syn_data

# from torchviz import make_dot
# import torch.nn.functional as Functional
# from torch.nn.parameter import Parameter

from torch.optim.lr_scheduler import _LRScheduler

#setting this off because I place this file in fbgemm_gpu's directory as a temporary workaround to acces fbgemm_gpu calls.
if True:
    #To build fbgemm_gpu, commit id 0fe80ee014b936733278a77d0a24c9fe9a431c31 was used.
    from fbgemm_gpu.split_table_batched_embeddings_ops import (
        CacheAlgorithm,
        ComputeDevice,
        EmbeddingLocation,
        OptimType,
        SparseType,
        SplitTableBatchedEmbeddingBagsCodegen,
        Int4TableBatchedEmbeddingBagsCodegen,
)

def infer_gpu(
    model, device, list_of_embedding_tables_tensors, num_embedding_rows, embed_dim, 
    num_tables, indices, offsets, lengths, quantize_emb, quantize_bits
    ):

    #if quantize_emb is False:
    #    # assume fp16
    #    model = model.half()

    offsets_original = offsets.detach().clone() #tensor
    indices_original = [e.detach().clone() for e in indices] #list

    prefix_sum_offsets = [0]
    prefix_sum_indices = [0]
    for i in range(num_tables):
        offsets[i] += prefix_sum_indices[i]
        indices[i] += i*num_embedding_rows
        prefix_sum_offsets.append(prefix_sum_offsets[-1] + len(offsets[i]))
        prefix_sum_indices.append(prefix_sum_indices[-1] + indices[i].shape[0])
    offsets = offsets.flatten().contiguous().view(-1).cuda()

    indices = torch.cat(indices, dim=0).cuda()
    indices = indices.long().contiguous().view(-1).cuda()
    
    ss = torch.tensor([indices.numel()]).cuda()
    offsets = torch.cat((offsets, ss))

    if quantize_bits == 4:
        for i in range(3):
            print("QUANTIZING 4!")
        model = Int4TableBatchedEmbeddingBagsCodegen(
                [(num_embedding_rows, embed_dim) for _ in range(num_tables)],
                #,
                list_of_embedding_tables_tensors
                ).cuda()
        #splits = model.splits()




        result = model.forward(
            indices.int(),
            offsets.int(),
        )
        #all_indices = torch.stack(indices, dim=0)
        #indices = all_indices.long().contiguous().view(-1).int().cuda()
        #print(indices)
        #all_lengths = torch.stack(lengths, dim=0).flatten()
        #offsets=torch.tensor(([0] + np.cumsum(all_lengths).tolist())).int().cuda()

    elif quantize_bits == 8:
        #for i in range(3):
        #    print("QUANTIZING 8!")        
        ###########################################################################################################
        ###########################################################################################################
        ###########################################################################################################
        ###########################################################################################################
        ###########################################################################################################

        #In Satish's docs, the number of offsets are  same per table? That would be needed to have the data ordering in
        # row#,table#,col#  instead of table#,row#,col#
        model = SplitTableBatchedEmbeddingBagsCodegen(
            [
                (
                    num_embedding_rows,
                    embed_dim - 8,
                    EmbeddingLocation.MANAGED_CACHING,
                    ComputeDevice.CUDA,
                )
                for _ in range(num_tables)
            ],
            weights_precision = SparseType.INT8,
            #feature_table_map = list_of_embedding_tables_tensors
        ).cuda()

        #Code is a slight modification of split_table_batched_embeddings_ops.py, def init_embedding_weights_uniform
        splits = model.split_embedding_weights()
        for i, emb in enumerate(splits):
            assert (
                len(emb.shape) == 2
            ), "Int8 embedding only supported for 2D weight tensors."            
            shape = [emb.shape[0], emb.shape[1]]# - model.int8_emb_row_dim_offset]
            tmp_emb = list_of_embedding_tables_tensors[i].cuda()

            emb.data[:,:].copy_(tmp_emb)
            #emb.data[:,:-(model.int8_emb_row_dim_offset)].copy_(tmp_emb)
            #tmp_emb_i8 = torch.ops.fb.FloatToFused8BitRowwiseQuantized(tmp_emb)
            #emb.data.copy_(tmp_emb_i8)


        result = model.forward(
            indices,
            offsets,
        )    
        torch.cuda.synchronize()
        #result is torch.Size([63, 408])
        #r = [result[:, i::num_tables] for i in range (num_tables)]
        r = [result[:, i:i+32] for i in range (num_tables)]
        #print(r[1][0])
        print(torch.cuda.memory_stats)
        return r
        #r = r.view( r.shape[0]*r.shape[1], r.shape[2])
        #result = [r[i,:] for i in range(r.shape[0])]
        #return result
        #all_indices = torch.stack(indices, dim=0)
        #indices = all_indices.long().contiguous().view(-1).cuda()
        #all_lengths = torch.stack(lengths, dim=0).flatten()
        #offsets=torch.tensor(([0] + np.cumsum(all_lengths).tolist())).long().cuda()
    else:
        for i in range(2):
            print("QUANTIZING else!")
        concatenated_embedding_tables = torch.zeros( num_tables * num_embedding_rows, embed_dim ).cuda()
        for i in range(num_tables):
            concatenated_embedding_tables[i*num_embedding_rows:(i+1)*num_embedding_rows,:] = list_of_embedding_tables_tensors[i].cuda()
        model = torch.nn.EmbeddingBag.from_pretrained(concatenated_embedding_tables, mode = 'sum').to(device)
        result = model(indices, offsets)
        #result is 192,128
    result = [ result[prefix_sum_offsets[i]:prefix_sum_offsets[i+1], :] for i in range (num_tables) ]

    #result = [result[i::num_tables, :] for i in range (num_tables)]
    #result = [ result[(i+prefix_sum_offsets[i]):(i+prefix_sum_offsets[i+1]):num_tables, :] for i in range (num_tables) ]

    return result

    for i in range(args.warmups):
        output = model(indices, offsets)
    torch.cuda.synchronize()
    start = time.time()
    for i in range(args.steps):
        output = model(indices, offsets)
    torch.cuda.synchronize()
    end = time.time()

    return (end - start)

exc = getattr(builtins, "IOError", "FileNotFoundError")

class LRPolicyScheduler(_LRScheduler):
    def __init__(self, optimizer, num_warmup_steps, decay_start_step, num_decay_steps):
        self.num_warmup_steps = num_warmup_steps
        self.decay_start_step = decay_start_step
        self.decay_end_step = decay_start_step + num_decay_steps
        self.num_decay_steps = num_decay_steps

        if self.decay_start_step < self.num_warmup_steps:
            sys.exit("Learning rate warmup must finish before the decay starts")

        super(LRPolicyScheduler, self).__init__(optimizer)

    def get_lr(self):
        step_count = self._step_count
        if step_count < self.num_warmup_steps:
            # warmup
            scale = 1.0 - (self.num_warmup_steps - step_count) / self.num_warmup_steps
            lr = [base_lr * scale for base_lr in self.base_lrs]
            self.last_lr = lr
        elif self.decay_start_step <= step_count and step_count < self.decay_end_step:
            # decay
            decayed_steps = step_count - self.decay_start_step
            scale = ((self.num_decay_steps - decayed_steps) / self.num_decay_steps) ** 2
            min_lr = 0.0000001
            lr = [max(min_lr, base_lr * scale) for base_lr in self.base_lrs]
            self.last_lr = lr
        else:
            if self.num_decay_steps > 0:
                # freeze at last, either because we're after decay
                # or because we're between warmup and decay
                lr = self.last_lr
            else:
                # do not adjust
                lr = self.base_lrs
        return lr

### define dlrm in PyTorch ###
class DLRM_Net(nn.Module):
    def create_mlp(self, ln, sigmoid_layer):
        # build MLP layer by layer
        layers = nn.ModuleList()
        for i in range(0, ln.size - 1):
            n = ln[i]
            m = ln[i + 1]

            # construct fully connected operator
            LL = nn.Linear(int(n), int(m), bias=True)

            # initialize the weights
            # with torch.no_grad():
            # custom Xavier input, output or two-sided fill
            mean = 0.0  # std_dev = np.sqrt(variance)
            std_dev = np.sqrt(2 / (m + n))  # np.sqrt(1 / m) # np.sqrt(1 / n)
            W = np.random.normal(mean, std_dev, size=(m, n)).astype(np.float32)
            std_dev = np.sqrt(1 / m)  # np.sqrt(2 / (m + 1))
            bt = np.random.normal(mean, std_dev, size=m).astype(np.float32)
            # approach 1
            LL.weight.data = torch.tensor(W, requires_grad=True)
            LL.bias.data = torch.tensor(bt, requires_grad=True)
            # approach 2
            # LL.weight.data.copy_(torch.tensor(W))
            # LL.bias.data.copy_(torch.tensor(bt))
            # approach 3
            # LL.weight = Parameter(torch.tensor(W),requires_grad=True)
            # LL.bias = Parameter(torch.tensor(bt),requires_grad=True)
            layers.append(LL)

            # construct sigmoid or relu operator
            if i == sigmoid_layer:
                layers.append(nn.Sigmoid())
            else:
                layers.append(nn.ReLU())

        # approach 1: use ModuleList
        # return layers
        # approach 2: use Sequential container to wrap all layers
        return torch.nn.Sequential(*layers)

    def create_emb(self, m, ln):   
        # to do: use docstring. Perhaps https://sphinx-rtd-tutorial.readthedocs.io/en/latest/docstrings.html ??
        # create_emb explained in plain english:
        #
        # ln parameter:
        # ln is a list of all the tables' row counts. E.g. [10,5,16] would mean table 0 has 10 rows, 
        # table 1 has 5 rows, and table 2 has 16 rows.
        # 
        # m parameter (when m is a single value):
        # m is the length of all embedding vectors. All embedding vectors in all embedding tables are 
        # created to be the same length. E.g. if ln were [3,2,5] and m were 4, table 0 would be dimension
        # 3 x 4, table 1 would be 2 x 4, and table 2 would be 5 x 4.
        # 
        # m parameter (when m is a list):
        # m is a list of all the tables' column counts. E.g. if m were [4,5,6] and ln were [3,2,5],
        # table 0 would be dimension 3 x 4, table 1 would be 2 x 5, and table 2 would be 5 x 6.
        # 
        # Key to remember:
        # embedding table i has shape: ln[i] rows, m columns, when m is a single value.
        # embedding table i has shape: ln[i] rows, m[i] columns, when m is a list.
        




        emb_l = nn.ModuleList()
            # save the numpy random state
        np_rand_state = np.random.get_state()
        for i in range(0, ln.size):
            if ext_dist.my_size > 1:
                if not i in self.local_emb_indices: continue
            # Use per table random seed for Embedding initialization
            np.random.seed(self.l_emb_seeds[i])
            n = ln[i]   # n stores the # of rows (i.e., embedding vectors) in table ln[i]
            # construct embedding operator
            if self.qr_flag and n > self.qr_threshold:
                EE = QREmbeddingBag(n, m, self.qr_collisions,
                    operation=self.qr_operation, mode="sum", sparse=True)
            elif self.md_flag:
                base = max(m)
                _m = m[i] if n > self.md_threshold else base   # I see m[i] here.. Is m[i] a list or a single value??
                EE = PrEmbeddingBag(n, _m, base)
                # use np initialization as below for consistency...
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, _m)
                ).astype(np.float32)
                EE.embs.weight.data = torch.tensor(W, requires_grad=True)

            else:
                #_weight = torch.empty([n, m]).uniform_(-np.sqrt(1 / n), np.sqrt(1 / n))
                #EE = nn.EmbeddingBag(n, m, mode="sum", sparse=True, _weight= _weight)
                #EE = nn.EmbeddingBag(n, m, mode="sum", sparse=True)

                # initialize embeddings
                # nn.init.uniform_(EE.weight, a=-np.sqrt(1 / n), b=np.sqrt(1 / n))
                W = np.random.uniform(
                    low=-np.sqrt(1 / n), high=np.sqrt(1 / n), size=(n, m)
                ).astype(np.float32)
                # approach 1
                EE = nn.EmbeddingBag(n, m, mode="sum", sparse=True, _weight=torch.tensor(W, requires_grad=True))
                #EE.weight.data = torch.tensor(W, requires_grad=True)
                # approach 2
                # EE.weight.data.copy_(torch.tensor(W))
                # approach 3
                # EE.weight = Parameter(torch.tensor(W),requires_grad=True)

            if ext_dist.my_size > 1:
                if i in self.local_emb_indices:
                    emb_l.append(EE)
            else:
                emb_l.append(EE)

        # Restore the numpy random state
        np.random.set_state(np_rand_state)
        return emb_l

    def __init__(
        self,
        m_spa=None,
        ln_emb=None,
        ln_bot=None,
        ln_top=None,
        proj_size = 0,
        arch_interaction_op=None,
        arch_interaction_itself=False,
        sigmoid_bot=-1,
        sigmoid_top=-1,
        sync_dense_params=True,
        loss_threshold=0.0,
        ndevices=-1,
        qr_flag=False,
        qr_operation="mult",
        qr_collisions=0,
        qr_threshold=200,
        md_flag=False,
        md_threshold=200,
    ):
        super(DLRM_Net, self).__init__()

        if (
            (m_spa is not None)
            and (ln_emb is not None)
            and (ln_bot is not None)
            and (ln_top is not None)
            and (arch_interaction_op is not None)
        ):

            # save arguments
            self.proj_size = proj_size
            self.ndevices = ndevices
            self.output_d = 0
            self.parallel_model_batch_size = -1
            self.parallel_model_is_not_prepared = True
            self.arch_interaction_op = arch_interaction_op
            self.arch_interaction_itself = arch_interaction_itself
            self.sync_dense_params = sync_dense_params
            self.loss_threshold = loss_threshold
            # create variables for QR embedding if applicable
            self.qr_flag = qr_flag          ### qt = quotient-remainder
            if self.qr_flag:
                self.qr_collisions = qr_collisions
                self.qr_operation = qr_operation
                self.qr_threshold = qr_threshold
            # create variables for MD embedding if applicable
            self.md_flag = md_flag
            if self.md_flag:                ### md = mixed dimensions
                self.md_threshold = md_threshold

            # generate np seeds for Emb table initialization
            self.l_emb_seeds = np.random.randint(low=0, high=100000, size=len(ln_emb))

            #If running distributed, get local slice of embedding tables
            if ext_dist.my_size > 1:
                n_emb = len(ln_emb)
                self.n_global_emb = n_emb
                self.n_local_emb, self.n_emb_per_rank = ext_dist.get_split_lengths(n_emb)
                self.local_emb_slice = ext_dist.get_my_slice(n_emb)
                self.local_emb_indices = list(range(n_emb))[self.local_emb_slice]
                #ln_emb = ln_emb[self.local_emb_slice]

            # create operators
            #######################################################################################################################
            #######################################################################################################################
            #######################################################################################################################
            #######################################################################################################################
            #######################################################################################################################            
            if ndevices <= 1:
                self.emb_l = self.create_emb(m_spa, ln_emb)  #self.emb_l stores list of nn.EmbeddingBag instantiations. There are ln_emb.size() instantiations. Each instantion's dimension is m_spa columns by ln_emb[i] rows.

            self.bot_l = self.create_mlp(ln_bot, sigmoid_bot)
            self.top_l = self.create_mlp(ln_top, sigmoid_top)
            if (proj_size > 0):
                self.proj_l = project.create_proj(len(ln_emb)+1, proj_size)            

            # quantization
            self.quantize_emb = False
            self.emb_l_q = []
            self.quantize_bits = 32            

    def apply_mlp(self, x, layers):
        # approach 1: use ModuleList
        # for layer in layers:
        #     x = layer(x)
        # return x
        # approach 2: use Sequential container to wrap all layers
        return layers(x)

    def apply_proj(self, x, layers):
        # approach 1: use ModuleList
        # for layer in layers:
        #     x = layer(x)
        # return x
        # approach 2: use Sequential container to wrap all layers
        return layers(x)

    def apply_emb(self, lS_o, lS_i, emb_l):
        # WARNING: notice that we are processing the batch at once. We implicitly
        # assume that the data is laid out such that:
        # 1. each embedding is indexed with a group of sparse indices,
        #   corresponding to a single lookup
        # 2. for each embedding the lookups are further organized into a batch
        # 3. for a list of embedding tables there is a list of batched lookups

        ly = []

        process_all_tables_as_a_single_tensor = True
        if process_all_tables_as_a_single_tensor:
            if emb_l is None:
                list_of_embedding_tables_tensors = self.emb_l_q
            else:
                list_of_embedding_tables_tensors =  [e.weight for e in emb_l]
            ly = infer_gpu(
                model = self, 
                device = next(self.parameters()).device, 
                list_of_embedding_tables_tensors = list_of_embedding_tables_tensors,
                num_embedding_rows = len(list_of_embedding_tables_tensors[0]),
                embed_dim = len(list_of_embedding_tables_tensors[0][0]),
                num_tables = len(list_of_embedding_tables_tensors),
                indices = lS_i,  #list of 1d tensors
                offsets = lS_o,  #2d tensor
                lengths = [x.shape[0] for x in lS_i], 
                quantize_emb = self.quantize_emb, 
                quantize_bits = self.quantize_bits #hard coded to 32 bit, from code forked from.
                )
        else:
            for k, sparse_index_group_batch in enumerate(lS_i):
                sparse_offset_group_batch = lS_o[k]

                # embedding lookup
                # We are using EmbeddingBag, which implicitly uses sum operator.
                # The embeddings are represented as tall matrices, with sum
                # happening vertically across 0 axis, resulting in a row vector
                per_sample_weights = None

                if self.quantize_emb:
                    s1 = self.emb_l_q[k].element_size() * self.emb_l_q[k].nelement()
                    s2 = self.emb_l_q[k].element_size() * self.emb_l_q[k].nelement()
                    #print("quantized emb sizes:", s1, s2)

                    if self.quantize_bits == 4:
                        QV = ops.quantized.embedding_bag_4bit_rowwise_offsets(
                            self.emb_l_q[k],
                            sparse_index_group_batch,
                            sparse_offset_group_batch,
                            per_sample_weights=per_sample_weights,
                        )
                    elif self.quantize_bits == 8:
                        QV = ops.quantized.embedding_bag_byte_rowwise_offsets(
                            self.emb_l_q[k],
                            sparse_index_group_batch,
                            sparse_offset_group_batch,
                            per_sample_weights=per_sample_weights,
                        )

                    ly.append(QV)
                else:
                    E = emb_l[k]
                    V = E(
                        sparse_index_group_batch,
                        sparse_offset_group_batch,
                        per_sample_weights=per_sample_weights,
                    )

                    ly.append(V)
        return ly

    #  using quantizing functions from caffe2/aten/src/ATen/native/quantized/cpu
    def quantize_embedding(self, bits):
        """
        emb = []
        emb.append(torch.FloatTensor([[-0.27341,  0.03442, -0.03392, -0.25681,  0.30706,  0.43672,  0.43053,
                -0.21731, -0.26392,  0.39838,  0.04191, -0.34124,  0.07877, -0.28752,
                -0.33985, -0.18040, -0.27464, -0.14082,  0.10969, -0.20656,  0.11433,
                -0.37716, -0.21056, -0.22694,  0.06149,  0.05674, -0.21970,  0.17155,
                -0.15404, -0.39372, -0.00655, -0.17976],
                [ 0.40799,  0.29757,  0.02161, -0.01177,  0.19130,  0.40685, -0.23482,
                0.20121, -0.40148,  0.39782,  0.21343,  0.35857, -0.36398, -0.14493,
                0.31090,  0.28369,  0.40169,  0.21992,  0.37128, -0.31763, -0.43709,
                0.43012,  0.06213, -0.42251,  0.36262,  0.31405,  0.02354, -0.19753,
                0.40378,  0.14292,  0.01299, -0.26330],
                [ 0.07249, -0.24687,  0.15673,  0.24212, -0.37316, -0.26838,  0.36272,
                0.27507, -0.40194,  0.03928,  0.10230,  0.31934,  0.32213,  0.11816,
                -0.30828, -0.28766,  0.44641,  0.16771,  0.01645, -0.20237,  0.16037,
                -0.16096, -0.21080,  0.02464, -0.05678,  0.42974,  0.39430, -0.29634,
                0.22931, -0.40895,  0.41226, -0.36954],
                [ 0.31674,  0.10682, -0.06666,  0.24304,  0.02657, -0.13466, -0.34582,
                -0.13631, -0.21489, -0.00432, -0.30249, -0.04384, -0.01568,  0.38206,
                -0.13085, -0.34548,  0.43824,  0.01145, -0.24154,  0.05578, -0.08942,
                0.41371,  0.19624,  0.19385,  0.24424, -0.17983, -0.26516, -0.44357,
                -0.21975,  0.07273,  0.38498, -0.24139],
                [ 0.40975,  0.38627, -0.27367,  0.12989,  0.39100, -0.21326, -0.15886,
                -0.35694,  0.16019, -0.42316,  0.18242, -0.43950, -0.44245,  0.28245,
                -0.16483, -0.42265,  0.13488, -0.27938,  0.03102,  0.40754, -0.01194,
                -0.36280,  0.11246, -0.24537,  0.22124, -0.28704,  0.07018,  0.40641,
                -0.04692, -0.12810, -0.19699,  0.29025]]))
        emb.append(torch.FloatTensor([[ 0.42850, -0.15664,  0.33396,  0.21020,  0.14055,  0.29015, -0.07323,
                -0.02302,  0.39116, -0.19163, -0.15280, -0.19715,  0.40195, -0.42736,
                -0.19377, -0.16732,  0.09551,  0.21136,  0.38371, -0.11261,  0.04176,
                0.40669, -0.13749,  0.15615,  0.02639, -0.41473,  0.02114,  0.31859,
                -0.02332, -0.23895, -0.41641,  0.42314],
                [ 0.02243, -0.19169,  0.18357,  0.01954,  0.20777, -0.08639,  0.12559,
                -0.35251, -0.07740, -0.14124,  0.40190,  0.22675, -0.14763,  0.30974,
                0.27002, -0.38648,  0.02151,  0.15901, -0.32810,  0.05379,  0.22291,
                -0.36109, -0.05040,  0.44711, -0.35668, -0.03231,  0.28806,  0.34521,
                0.44404, -0.35927, -0.17417,  0.19439],
                [ 0.29188, -0.33870,  0.33787,  0.22743,  0.00377, -0.32600, -0.08054,
                -0.35348, -0.29372, -0.11703,  0.16246, -0.26498,  0.33385,  0.10457,
                -0.03544, -0.38472, -0.20837, -0.41963,  0.34716, -0.22655, -0.18551,
                0.34825, -0.38426,  0.06559,  0.12975,  0.36548,  0.37557,  0.42535,
                -0.09962,  0.42649,  0.03438,  0.36743],
                [-0.42871, -0.28256,  0.21452, -0.22649, -0.06022, -0.33893, -0.18209,
                -0.24851,  0.04371,  0.12766,  0.05841, -0.09600,  0.15212, -0.15409,
                0.41725, -0.21561, -0.32636, -0.07655, -0.36751,  0.13858,  0.08966,
                -0.11541, -0.28887, -0.31499, -0.04713,  0.42586,  0.13693,  0.36753,
                -0.08073, -0.29961, -0.36387, -0.05154],
                [ 0.02506,  0.28888,  0.15628, -0.11286, -0.40579,  0.10700,  0.08654,
                -0.24092,  0.26153,  0.04785,  0.42795,  0.05007,  0.14691, -0.13211,
                -0.36267,  0.11879,  0.31046, -0.33505, -0.21442, -0.13820,  0.15759,
                0.17402,  0.15727, -0.29780, -0.09069,  0.07474, -0.01198, -0.11333,
                -0.39380, -0.32731,  0.36327, -0.03212]]))
        emb.append(torch.FloatTensor([[ 0.01758, -0.09590,  0.32071, -0.43757, -0.00166,  0.17116, -0.42252,
                0.04860, -0.25584, -0.19813, -0.19334, -0.31393,  0.28279,  0.20648,
                -0.02019,  0.05096, -0.33818,  0.40305, -0.36846,  0.00699, -0.21815,
                0.14217, -0.11431,  0.30278,  0.35357, -0.26305,  0.24241, -0.17799,
                0.08360, -0.02432, -0.30774,  0.12134],
                [-0.28493, -0.02226,  0.35828,  0.23104, -0.32100,  0.03855, -0.06948,
                0.06764, -0.10200,  0.00879,  0.07726, -0.26149, -0.13608,  0.20249,
                -0.18496, -0.43853, -0.16563,  0.26291,  0.22624, -0.14405, -0.25083,
                0.13123,  0.01462,  0.12318,  0.08294,  0.04881,  0.36968,  0.10914,
                -0.22338, -0.41087,  0.13521, -0.15243],
                [ 0.03405,  0.00803, -0.04823,  0.25976, -0.08477, -0.25676, -0.30863,
                0.36584, -0.28705, -0.36133,  0.29937,  0.34463,  0.06512, -0.24379,
                0.10290,  0.30022,  0.41155,  0.09330, -0.26728,  0.44208,  0.32442,
                0.32123, -0.07328, -0.33121, -0.32177, -0.40126,  0.08831, -0.42180,
                -0.07408, -0.05753,  0.39945,  0.28790],
                [-0.03429, -0.04978,  0.04406,  0.19969,  0.10809, -0.14287,  0.43690,
                -0.21308, -0.43638, -0.25834,  0.26456,  0.15024,  0.37073,  0.23751,
                0.44603,  0.42768, -0.30522, -0.44395,  0.26790,  0.11635, -0.23647,
                -0.25693,  0.44107, -0.33589,  0.09012, -0.28107,  0.25106,  0.40811,
                0.11480,  0.19355, -0.19726,  0.19189],
                [ 0.05995, -0.18809,  0.16625, -0.08336, -0.39570,  0.33361, -0.36259,
                -0.34752, -0.17453, -0.31968,  0.17745, -0.17284, -0.33616,  0.27911,
                -0.10976,  0.03157,  0.16658,  0.07527,  0.07013, -0.18617, -0.08529,
                -0.29439,  0.29183, -0.16394, -0.06173,  0.36814, -0.19531, -0.07184,
                0.02368, -0.10256,  0.11800,  0.14188]]))
        for i in range(3):
            emb[i].data[:,:] = 1.0
            self.emb_l[i].weight.data = emb[i].data
        """
        n = len(self.emb_l)
        self.emb_l_q = [None] * n
        for k in range(n):
            if bits == 4:
                self.emb_l_q[k] = ops.quantized.embedding_bag_4bit_prepack(
                    self.emb_l[k].weight
                )

            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
            

            elif bits == 8:
                self.emb_l_q[k] = ops.quantized.embedding_bag_byte_prepack(
                    self.emb_l[k].weight
                )
            else:
                return
        self.emb_l = None
        self.quantize_emb = True
        self.quantize_bits = bits
    def interact_features(self, x, ly):
            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
            ##############################################################################################################
                  
        if self.arch_interaction_op == "dot":
            # concatenate dense and sparse features
            (batch_size, d) = x.shape
            T = torch.cat([x] + ly, dim=1).view((batch_size, -1, d))
            # perform a dot product
            if (self.proj_size > 0):
                R = project.project(T, x, self.proj_l)
                #TT = torch.transpose(T, 1, 2)
                #TS = torch.reshape(TT, (-1, TT.size(2)))
                #TC = self.apply_mlp(TS, self.proj_l)
                #TR = torch.reshape(TC, (-1, d ,self.proj_size))
                #Z  = torch.bmm(T, TR)
                #Zflat = Z.view((batch_size, -1))
                #R = torch.cat([x] + [Zflat], dim=1)
            else:            
                Z = torch.bmm(T, torch.transpose(T, 1, 2))
                # append dense feature with the interactions (into a row vector)
                # approach 1: all
                # Zflat = Z.view((batch_size, -1))
                # approach 2: unique
                _, ni, nj = Z.shape
                # approach 1: tril_indices
                # offset = 0 if self.arch_interaction_itself else -1
                # li, lj = torch.tril_indices(ni, nj, offset=offset)
                # approach 2: custom
                offset = 1 if self.arch_interaction_itself else 0
                li = torch.tensor([i for i in range(ni) for j in range(i + offset)])
                lj = torch.tensor([j for i in range(nj) for j in range(i + offset)])
                Zflat = Z[:, li, lj]
                # concatenate dense features and interactions
                R = torch.cat([x] + [Zflat], dim=1)
        elif self.arch_interaction_op == "cat":
            # concatenation features (into a row vector)
            R = torch.cat([x] + ly, dim=1)
        else:
            sys.exit(
                "ERROR: --arch-interaction-op="
                + self.arch_interaction_op
                + " is not supported"
            )

        return R

    def forward(self, dense_x, lS_o, lS_i):
        if ext_dist.my_size > 1:
            return self.distributed_forward(dense_x, lS_o, lS_i)
        elif self.ndevices <= 1:
            return self.sequential_forward(dense_x, lS_o, lS_i)
        else:
            return self.parallel_forward(dense_x, lS_o, lS_i)

    ####################################################################################################
    ####################################################################################################
    ####################################################################################################
    ####################################################################################################
    ####################################################################################################
    ####################################################################################################
    def sequential_forward(self, dense_x, lS_o, lS_i):
        #OFFSETS!!!!
        if False:
            lS_o_test = torch.LongTensor([[ 0,  4,  9, 14, 17, 22, 27, 31, 36, 40, 44, 49, 54, 58, 63, 68],
            [ 0,  4,  9, 13, 18, 22, 27, 32, 37, 42, 47, 52, 57, 62, 67, 72],
            [ 0,  5, 10, 15, 20, 25, 29, 33, 38, 43, 48, 53, 58, 63, 68, 73]]).cuda()
            self.lS_o = torch.clone(lS_o_test)
            lS_o = self.lS_o

            indexes = []
            indexes.append(torch.LongTensor([1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 1, 2, 3, 0, 1, 2, 3, 4, 0, 1,
                    2, 3, 4, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 0, 1, 2, 3, 0, 1, 2, 3,
                    4, 0, 1, 2, 3, 4, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3,
                    4]).cuda())
            indexes.append(torch.LongTensor([0, 1, 2, 3, 0, 1, 2, 3, 4, 0, 1, 2, 3, 0, 1, 2, 3, 4, 0, 1, 2, 3, 0, 1,
                    2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0,
                    1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4,
                    0, 1, 2, 3, 4]).cuda())
            indexes.append(torch.LongTensor([0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3,
                    4, 1, 2, 3, 4, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4,
                    0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3, 4, 0, 1, 2, 3,
                    4, 0, 1, 2, 3, 4]).cuda())        
            for e in indexes:
                for i in range(len(e)):
                    e[i] = 4*i // len(e)
            self.lS_i = indexes.copy()
            lS_i = self.lS_i

        # process dense features (using bottom mlp), resulting in a row vector
        x = self.apply_mlp(dense_x, self.bot_l)
        # debug prints
        # print("intermediate")
        # print(x.detach().cpu().numpy())

        # process sparse features(using embeddings), resulting in a list of row vectors
        ly = self.apply_emb(lS_o, lS_i, self.emb_l)
        # for y in ly:
        #     print(y.detach().cpu().numpy())

        # interact features (dense and sparse)
        z = self.interact_features(x, ly)
        # print(z.detach().cpu().numpy())

        # obtain probability of a click (using top mlp)
        p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z = torch.clamp(p, min=self.loss_threshold, max=(1.0 - self.loss_threshold))
        else:
            z = p

        return z
    
    def distributed_forward(self, dense_x, lS_o, lS_i):
        batch_size = dense_x.size()[0]
        # WARNING: # of ranks must be <= batch size in distributed_forward call
        if batch_size < ext_dist.my_size:
            sys.exit("ERROR: batch_size (%d) must be larger than number of ranks (%d)" % (batch_size, ext_dist.my_size))
        if batch_size % ext_dist.my_size != 0:
            sys.exit("ERROR: batch_size %d can not split across %d ranks evenly" % (batch_size, ext_dist.my_size))

        dense_x = dense_x[ext_dist.get_my_slice(batch_size)]
        lS_o = lS_o[self.local_emb_slice]
        lS_i = lS_i[self.local_emb_slice]

        if (len(self.emb_l) != len(lS_o)) or (len(self.emb_l) != len(lS_i)):
            sys.exit("ERROR: corrupted model input detected in distributed_forward call")

        # embeddings
        ly = self.apply_emb(lS_o, lS_i, self.emb_l)
        # print("ly: ", ly)
        # debug prints
        # print(ly)

        # WARNING: Note that at this point we have the result of the embedding lookup
        # for the entire batch on each rank. We would like to obtain partial results
        # corresponding to all embedding lookups, but part of the batch on each rank.
        # Therefore, matching the distribution of output of bottom mlp, so that both
        # could be used for subsequent interactions on each device.
        if len(self.emb_l) != len(ly):
            sys.exit("ERROR: corrupted intermediate result in distributed_forward call")

        a2a_req = ext_dist.alltoall(ly, self.n_emb_per_rank)

        x = self.apply_mlp(dense_x, self.bot_l)
        # debug prints
        # print(x)

        ly = a2a_req.wait()
        # print("ly: ", ly)
        ly = list(ly)

        # interactions
        z = self.interact_features(x, ly)
        # debug prints
        # print(z)

        # top mlp
        p = self.apply_mlp(z, self.top_l)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z = torch.clamp(
                p, min=self.loss_threshold, max=(1.0 - self.loss_threshold)
            )
        else:
            z = p

        ### gather the distributed results on each rank ###
        # For some reason it requires explicit sync before all_gather call if
        # tensor is on GPU memory
        if z.is_cuda: torch.cuda.synchronize()
        (_, batch_split_lengths) = ext_dist.get_split_lengths(batch_size)
        z = ext_dist.all_gather(z, batch_split_lengths)
        #print("Z: %s" % z)

        return z

    def parallel_forward(self, dense_x, lS_o, lS_i):
        ### prepare model (overwrite) ###
        # WARNING: # of devices must be >= batch size in parallel_forward call
        batch_size = dense_x.size()[0]
        ndevices = min(self.ndevices, batch_size, len(self.emb_l))
        device_ids = range(ndevices)
        # WARNING: must redistribute the model if mini-batch size changes(this is common
        # for last mini-batch, when # of elements in the dataset/batch size is not even
        if self.parallel_model_batch_size != batch_size:
            self.parallel_model_is_not_prepared = True

        if self.parallel_model_is_not_prepared or self.sync_dense_params:
            # replicate mlp (data parallelism)
            self.bot_l_replicas = replicate(self.bot_l, device_ids)
            self.top_l_replicas = replicate(self.top_l, device_ids)
            self.parallel_model_batch_size = batch_size

        if self.parallel_model_is_not_prepared:
            # distribute embeddings (model parallelism)
            t_list = []
            for k, emb in enumerate(self.emb_l):
                d = torch.device("cuda:" + str(k % ndevices))
                emb.to(d)
                t_list.append(emb.to(d))
            self.emb_l = nn.ModuleList(t_list)
            self.parallel_model_is_not_prepared = False

        ### prepare input (overwrite) ###
        # scatter dense features (data parallelism)
        # print(dense_x.device)
        dense_x = scatter(dense_x, device_ids, dim=0)
        # distribute sparse features (model parallelism)
        if (len(self.emb_l) != len(lS_o)) or (len(self.emb_l) != len(lS_i)):
            sys.exit("ERROR: corrupted model input detected in parallel_forward call")

        t_list = []
        i_list = []
        for k, _ in enumerate(self.emb_l):
            d = torch.device("cuda:" + str(k % ndevices))
            t_list.append(lS_o[k].to(d))
            i_list.append(lS_i[k].to(d))
        lS_o = t_list
        lS_i = i_list

        ### compute results in parallel ###
        # bottom mlp
        # WARNING: Note that the self.bot_l is a list of bottom mlp modules
        # that have been replicated across devices, while dense_x is a tuple of dense
        # inputs that has been scattered across devices on the first (batch) dimension.
        # The output is a list of tensors scattered across devices according to the
        # distribution of dense_x.
        x = parallel_apply(self.bot_l_replicas, dense_x, None, device_ids)
        # debug prints
        # print(x)

        # embeddings
        ly = self.apply_emb(lS_o, lS_i, self.emb_l)
        # debug prints
        # print(ly)

        # butterfly shuffle (implemented inefficiently for now)
        # WARNING: Note that at this point we have the result of the embedding lookup
        # for the entire batch on each device. We would like to obtain partial results
        # corresponding to all embedding lookups, but part of the batch on each device.
        # Therefore, matching the distribution of output of bottom mlp, so that both
        # could be used for subsequent interactions on each device.
        if len(self.emb_l) != len(ly):
            sys.exit("ERROR: corrupted intermediate result in parallel_forward call")

        t_list = []
        for k, _ in enumerate(self.emb_l):
            d = torch.device("cuda:" + str(k % ndevices))
            y = scatter(ly[k], device_ids, dim=0)
            t_list.append(y)
        # adjust the list to be ordered per device
        ly = list(map(lambda y: list(y), zip(*t_list)))
        # debug prints
        # print(ly)

        # interactions
        z = []
        for k in range(ndevices):
            zk = self.interact_features(x[k], ly[k])
            z.append(zk)
        # debug prints
        # print(z)

        # top mlp
        # WARNING: Note that the self.top_l is a list of top mlp modules that
        # have been replicated across devices, while z is a list of interaction results
        # that by construction are scattered across devices on the first (batch) dim.
        # The output is a list of tensors scattered across devices according to the
        # distribution of z.
        p = parallel_apply(self.top_l_replicas, z, None, device_ids)

        ### gather the distributed results ###
        p0 = gather(p, self.output_d, dim=0)

        # clamp output if needed
        if 0.0 < self.loss_threshold and self.loss_threshold < 1.0:
            z0 = torch.clamp(
                p0, min=self.loss_threshold, max=(1.0 - self.loss_threshold)
            )
        else:
            z0 = p0

        return z0


def dash_separated_ints(value):
    vals = value.split('-')
    for val in vals:
        try:
            int(val)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "%s is not a valid dash separated list of ints" % value)

    return value


def dash_separated_floats(value):
    vals = value.split('-')
    for val in vals:
        try:
            float(val)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "%s is not a valid dash separated list of floats" % value)

    return value


if __name__ == "__main__":
    ### import packages ###
    import sys
    import os
    import argparse

    ### parse arguments ###
    parser = argparse.ArgumentParser(
        description="Train Deep Learning Recommendation Model (DLRM)"
    )
    # model related parameters
    parser.add_argument("--arch-sparse-feature-size", type=int, default=2)

    parser.add_argument(
        "--arch-embedding-size", type=dash_separated_ints, default="4-3-2")
    parser.add_argument("--arch-project-size", type=int, default=0)

    # j will be replaced with the table number
    parser.add_argument(
        "--arch-mlp-bot", type=dash_separated_ints, default="4-3-2")
    parser.add_argument(
        "--arch-mlp-top", type=dash_separated_ints, default="4-2-1")
    parser.add_argument(
        "--arch-interaction-op", type=str, choices=['dot', 'cat'], default="dot")
    parser.add_argument("--arch-interaction-itself", action="store_true", default=False)
    # embedding table options
    parser.add_argument("--md-flag", action="store_true", default=False)
    parser.add_argument("--md-threshold", type=int, default=200)
    parser.add_argument("--md-temperature", type=float, default=0.3)
    parser.add_argument("--md-round-dims", action="store_true", default=False)
    parser.add_argument("--qr-flag", action="store_true", default=False)
    parser.add_argument("--qr-threshold", type=int, default=200)
    parser.add_argument("--qr-operation", type=str, default="mult")
    parser.add_argument("--qr-collisions", type=int, default=4)
    # activations and loss
    parser.add_argument("--activation-function", type=str, default="relu")
    parser.add_argument("--loss-function", type=str, default="mse")  # or bce or wbce
    parser.add_argument("--loss-weights", type=str, default="1.0-1.0")  # for wbce
    parser.add_argument("--loss-threshold", type=float, default=0.0)  # 1.0e-7
    parser.add_argument("--round-targets", type=bool, default=False)
    # data
    parser.add_argument("--data-size", type=int, default=1)
    parser.add_argument("--num-batches", type=int, default=0)
    parser.add_argument(
        "--data-generation", type=str, default="random"
    )  # synthetic or dataset
    parser.add_argument("--synthetic-data-folder", type=str,
        default="./synthetic_data/syn_data_bs65536")
    # add Gaussian distribution
    parser.add_argument("--rand-data-dist", type=str, default="uniform")  # uniform or gaussian
    parser.add_argument("--rand-data-min", type=float, default=0)
    parser.add_argument("--rand-data-max", type=float, default=1)
    parser.add_argument("--rand-data-mu", type=float, default=-1)
    parser.add_argument("--rand-data-sigma", type=float, default=1)

    parser.add_argument("--data-trace-file", type=str, default="./input/dist_emb_j.log")
    parser.add_argument("--data-set", type=str, default="kaggle")  # or terabyte
    parser.add_argument("--raw-data-file", type=str, default="")
    parser.add_argument("--processed-data-file", type=str, default="")
    parser.add_argument("--data-randomize", type=str, default="total")  # or day or none
    parser.add_argument("--data-trace-enable-padding", type=bool, default=False)
    parser.add_argument("--max-ind-range", type=int, default=-1)
    parser.add_argument("--data-sub-sample-rate", type=float, default=0.0)  # in [0, 1]
    parser.add_argument("--num-indices-per-lookup", type=int, default=10)
    parser.add_argument("--num-indices-per-lookup-fixed", type=bool, default=False)
    parser.add_argument("--num-workers", type=int, default=0)
    parser.add_argument("--memory-map", action="store_true", default=False)
    # training
    parser.add_argument("--mini-batch-size", type=int, default=1)
    parser.add_argument("--nepochs", type=int, default=1)
    parser.add_argument("--learning-rate", type=float, default=0.01)
    parser.add_argument("--print-precision", type=int, default=5)
    parser.add_argument("--numpy-rand-seed", type=int, default=123)
    parser.add_argument("--sync-dense-params", type=bool, default=True)
    # inference
    parser.add_argument("--inference-only", action="store_true", default=False)
    # quantize
    parser.add_argument("--quantize-mlp-with-bit", type=int, default=32)
    parser.add_argument("--quantize-emb-with-bit", type=int, default=32)
    # onnx
    parser.add_argument("--save-onnx", action="store_true", default=False)
    # gpu
    parser.add_argument("--use-gpu", action="store_true", default=False)
    # distributed run
    parser.add_argument("--dist-backend", type=str, default="")
    # debugging and profiling
    parser.add_argument("--print-freq", type=int, default=1)
    parser.add_argument("--test-freq", type=int, default=-1)
    parser.add_argument("--test-mini-batch-size", type=int, default=-1)
    parser.add_argument("--test-num-workers", type=int, default=-1)
    parser.add_argument("--print-time", action="store_true", default=False)
    parser.add_argument("--debug-mode", action="store_true", default=False)
    parser.add_argument("--enable-profiling", action="store_true", default=False)
    parser.add_argument("--plot-compute-graph", action="store_true", default=False)
    # store/load model
    parser.add_argument("--out-dir", type=str, default=".")
    parser.add_argument("--save-model", type=str, default="")
    parser.add_argument("--load-model", type=str, default="")
    # mlperf logging (disables other output and stops early)
    parser.add_argument("--mlperf-logging", action="store_true", default=False)
    # stop at target accuracy Kaggle 0.789, Terabyte (sub-sampled=0.875) 0.8107
    parser.add_argument("--mlperf-acc-threshold", type=float, default=0.0)
    # stop at target AUC Terabyte (no subsampling) 0.8025
    parser.add_argument("--mlperf-auc-threshold", type=float, default=0.0)
    parser.add_argument("--mlperf-bin-loader", action='store_true', default=False)
    parser.add_argument("--mlperf-bin-shuffle", action='store_true', default=False)

    # LR policy
    parser.add_argument("--lr-num-warmup-steps", type=int, default=0)
    parser.add_argument("--lr-decay-start-step", type=int, default=0)
    parser.add_argument("--lr-num-decay-steps", type=int, default=0)


    #no_concat
    #concat_temp
    #concat_onward
    #are the emb tables concatenated, or the sparse feature vectors inputted to the tables?
    parser.add_argument("--concat_emb_tables", type=str, default="no_concat")


    args = parser.parse_args()

    print(socket.gethostname())

    ext_dist.init_distributed(backend=args.dist_backend)

    # print("success size= ", ext_dist.my_size, ext_dist.my_rank)

    ext_dist.barrier()

    if args.mlperf_logging:
        print('command line args: ', json.dumps(vars(args)))

    if args.quantize_emb_with_bit in [4, 8]:
        if args.qr_flag:
            sys.exit(
                "ERROR: 4 and 8-bit quantization with quotient remainder is not supported"
            )
        if args.md_flag:
            sys.exit(
                "ERROR: 4 and 8-bit quantization with mixed dimensions is not supported"
            )
    ### some basic setup ###
    np.random.seed(args.numpy_rand_seed)
    np.set_printoptions(precision=args.print_precision)
    torch.set_printoptions(precision=args.print_precision)
    torch.manual_seed(args.numpy_rand_seed)

    if (args.test_mini_batch_size < 0):
        # if the parameter is not set, use the training batch size
        args.test_mini_batch_size = args.mini_batch_size
    if (args.test_num_workers < 0):
        # if the parameter is not set, use the same parameter for training
        args.test_num_workers = args.num_workers
    if args.mini_batch_size % ext_dist.my_size !=0 or args.test_mini_batch_size % ext_dist.my_size != 0:
        print("Either test minibatch (%d) or train minibatch (%d) does not split across %d ranks" % (args.test_mini_batch_size, args.mini_batch_size, ext_dist.my_size))
        sys.exit(1)

    use_gpu = args.use_gpu and torch.cuda.is_available()
    if use_gpu:
        torch.cuda.manual_seed_all(args.numpy_rand_seed)
        torch.backends.cudnn.deterministic = True
        if ext_dist.my_size > 1:
            ngpus = torch.cuda.device_count()  # 1
            if ext_dist.my_local_size > torch.cuda.device_count():
                print("Not sufficient GPUs available... local_size = %d, ngpus = %d" % (ext_dist.my_local_size, ngpus))
                sys.exit(1)
            ngpus = 1
            device = torch.device("cuda", ext_dist.my_local_rank)
        else:
            device = torch.device("cuda", 0)
            ngpus = torch.cuda.device_count()  # 1
            ngpus=1
        print("Using {} GPU(s)...".format(ngpus))
    else:
        device = torch.device("cpu")
        print("Using CPU...")

    ### prepare training data ###
    ln_bot = np.fromstring(args.arch_mlp_bot, dtype=int, sep="-")
    # input data
    if (args.data_generation == "dataset"):

        train_data, train_ld, test_data, test_ld = \
            dp.make_criteo_data_and_loaders(args)
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        nbatches_test = len(test_ld)

        ln_emb = train_data.counts
        # enforce maximum limit on number of vectors per embedding
        if args.max_ind_range > 0:
            ln_emb = np.array(list(map(
                lambda x: x if x < args.max_ind_range else args.max_ind_range,
                ln_emb
            )))
        m_den = train_data.m_den
        ln_bot[0] = m_den

    elif args.data_generation == "synthetic":
        # input and target at random
        ln_emb = np.fromstring(args.arch_embedding_size, dtype=int, sep="-")
        m_den = ln_bot[0]
        train_data, train_ld = dd.data_loader(args, ln_emb, m_den)
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)
        table_feature_map = None #  {idx : idx for idx in range(len(ln_emb))}  
              
    else:
        # input and target at random
        ln_emb = np.fromstring(args.arch_embedding_size, dtype=int, sep="-")
        m_den = ln_bot[0]
        train_data, train_ld = dd.make_random_data_and_loader(args, ln_emb, m_den)
        nbatches = args.num_batches if args.num_batches > 0 else len(train_ld)


        #check that all lS_o are the same shape.
        #for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
        #    print("lS_o.shape",lS_o.shape)


    ### parse command line arguments ###
    m_spa = args.arch_sparse_feature_size
    num_fea = ln_emb.size + 1  # num sparse + num dense features
    m_den_out = ln_bot[ln_bot.size - 1]
    if args.arch_interaction_op == "dot":
        # approach 1: all
        # num_int = num_fea * num_fea + m_den_out
        # approach 2: unique
        if (args.arch_project_size > 0):
            num_int = num_fea * args.arch_project_size + m_den_out
        else:        
            if args.arch_interaction_itself:
                num_int = (num_fea * (num_fea + 1)) // 2 + m_den_out
            else:
                num_int = (num_fea * (num_fea - 1)) // 2 + m_den_out
    elif args.arch_interaction_op == "cat":
        num_int = num_fea * m_den_out
    else:
        sys.exit(
            "ERROR: --arch-interaction-op="
            + args.arch_interaction_op
            + " is not supported"
        )
    arch_mlp_top_adjusted = str(num_int) + "-" + args.arch_mlp_top
    ln_top = np.fromstring(arch_mlp_top_adjusted, dtype=int, sep="-")

    # sanity check: feature sizes and mlp dimensions must match
    if m_den != ln_bot[0]:
        sys.exit(
            "ERROR: arch-dense-feature-size "
            + str(m_den)
            + " does not match first dim of bottom mlp "
            + str(ln_bot[0])
        )
    if args.qr_flag:
        if args.qr_operation == "concat" and 2 * m_spa != m_den_out:
            sys.exit(
                "ERROR: 2 arch-sparse-feature-size "
                + str(2 * m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
                + " (note that the last dim of bottom mlp must be 2x the embedding dim)"
            )
        if args.qr_operation != "concat" and m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    else:
        if m_spa != m_den_out:
            sys.exit(
                "ERROR: arch-sparse-feature-size "
                + str(m_spa)
                + " does not match last dim of bottom mlp "
                + str(m_den_out)
            )
    if num_int != ln_top[0]:
        sys.exit(
            "ERROR: # of feature interactions "
            + str(num_int)
            + " does not match first dimension of top mlp "
            + str(ln_top[0])
        )

    # assign mixed dimensions if applicable
    if args.md_flag:
        m_spa = md_solver(
            torch.tensor(ln_emb),
            args.md_temperature,  # alpha
            d0=m_spa,
            round_dim=args.md_round_dims
        ).tolist()

    # test prints (model arch)
    if args.debug_mode:
        print("model arch:")
        print(
            "mlp top arch "
            + str(ln_top.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_top)
        print("# of interactions")
        print(num_int)
        print(
            "mlp bot arch "
            + str(ln_bot.size - 1)
            + " layers, with input to output dimensions:"
        )
        print(ln_bot)
        print("# of features (sparse and dense)")
        print(num_fea)
        print("dense feature size")
        print(m_den)
        print("sparse feature size")
        print(m_spa)
        print(
            "# of embeddings (= # of sparse features) "
            + str(ln_emb.size)
            + ", with dimensions "
            + str(m_spa)
            + "x:"
        )
        print(ln_emb)

        print("data (inputs and targets):")
        for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
            # early exit if nbatches was set by the user and has been exceeded
            if nbatches > 0 and j >= nbatches:
                break

            print("mini-batch: %d" % j)
            print(X.detach().cpu().numpy())
            # transform offsets to lengths when printing
            print(
                [
                    np.diff(
                        S_o.detach().cpu().tolist() + list(lS_i[i].shape)
                    ).tolist()
                    for i, S_o in enumerate(lS_o)
                ]
            )
            print([S_i.detach().cpu().tolist() for S_i in lS_i])
            print(T.detach().cpu().numpy())

    ndevices = min(ngpus, args.mini_batch_size, num_fea - 1) if use_gpu else -1

    torch.no_grad()

    ### construct the neural network specified above ###
    # WARNING: to obtain exactly the same initialization for
    # the weights we need to start from the same random seed.
    # np.random.seed(args.numpy_rand_seed)

    #####################################################################################################################
    #####################################################################################################################
    #####################################################################################################################
    #####################################################################################################################
    #####################################################################################################################
    dlrm = DLRM_Net(
        m_spa,
        ln_emb,
        ln_bot,
        ln_top,
        args.arch_project_size,
        arch_interaction_op=args.arch_interaction_op,
        arch_interaction_itself=args.arch_interaction_itself,
        sigmoid_bot=-1,
        sigmoid_top=ln_top.size - 2,
        sync_dense_params=args.sync_dense_params,
        loss_threshold=args.loss_threshold,
        ndevices=ndevices,
        qr_flag=args.qr_flag,
        qr_operation=args.qr_operation,
        qr_collisions=args.qr_collisions,
        qr_threshold=args.qr_threshold,
        md_flag=args.md_flag,
        md_threshold=args.md_threshold,
        #should DLRM contain a self.device variable?
    )
    # test prints
    if args.debug_mode:
        print("initial parameters (weights and bias):")
        for param in dlrm.parameters():
            print(param.detach().cpu().numpy())
        # print(dlrm)


    if args.inference_only:
        # Currently only dynamic quantization with INT8 and FP16 weights are
        # supported for MLPs and INT4 and INT8 weights for EmbeddingBag
        # post-training quantization during the inference.
        # By default we don't do the quantization: quantize_{mlp,emb}_with_bit == 32 (FP32)

        #####################################################################################################################################
        #####################################################################################################################################
        #####################################################################################################################################
        #####################################################################################################################################
        #   QUANTIZING THE EMBEDDINGS!!        
        assert args.quantize_mlp_with_bit in [
            8,
            16,
            32,
        ], "only support 8/16/32-bit but got {}".format(args.quantize_mlp_with_bit)
        assert args.quantize_emb_with_bit in [
            4,
            8,
            32,
        ], "only support 4/8/32-bit but got {}".format(args.quantize_emb_with_bit)
        if args.quantize_mlp_with_bit != 32:
            if args.quantize_mlp_with_bit in [8]:
                quantize_dtype = torch.qint8
            else:
                quantize_dtype = torch.float16
            dlrm = torch.quantization.quantize_dynamic(
                dlrm, {torch.nn.Linear}, quantize_dtype
            )
        if args.quantize_emb_with_bit != 32:
            dlrm.quantize_embedding(args.quantize_emb_with_bit)
            # print(dlrm)


    if use_gpu:
        # Custom Model-Data Parallel
        # the mlps are replicated and use data parallelism, while
        # the embeddings are distributed and use model parallelism
        ##############################################################################################################################        
        ##############################################################################################################################
        ##############################################################################################################################
        ##############################################################################################################################
        ##############################################################################################################################
        if dlrm.ndevices > 1:
            dlrm.emb_l = dlrm.create_emb(m_spa, ln_emb) ##############################################################################
        dlrm = dlrm.to(device)  # .cuda()
            
    if ext_dist.my_size > 1:
        if use_gpu:
            device_ids = [ext_dist.my_local_rank]
            dlrm.bot_l = DDP(dlrm.bot_l, device_ids=device_ids)
            dlrm.top_l = DDP(dlrm.top_l, device_ids=device_ids)
        else:
            dlrm.bot_l = DDP(dlrm.bot_l)
            dlrm.top_l = DDP(dlrm.top_l)

    # specify the loss function
    if args.loss_function == "mse":
        loss_fn = torch.nn.MSELoss(reduction="mean")
    elif args.loss_function == "bce":
        loss_fn = torch.nn.BCELoss(reduction="mean")
    elif args.loss_function == "wbce":
        loss_ws = torch.tensor(np.fromstring(args.loss_weights, dtype=float, sep="-"))
        loss_fn = torch.nn.BCELoss(reduction="none")
    else:
        sys.exit("ERROR: --loss-function=" + args.loss_function + " is not supported")

    if not args.inference_only:
        # specify the optimizer algorithm

        if ext_dist.my_size == 1:
            optimizer = torch.optim.SGD(dlrm.parameters(), lr=args.learning_rate)
            #lr_scheduler = LRPolicyScheduler(optimizer, args.lr_num_warmup_steps, args.lr_decay_start_step,
            #                                 args.lr_num_decay_steps)
        else:
            optimizer = torch.optim.SGD([
                {"params": [p for emb in dlrm.emb_l for p in emb.parameters()], "lr" : args.learning_rate},
                {"params": dlrm.bot_l.parameters(), "lr" : args.learning_rate * ext_dist.my_size},
                {"params": dlrm.top_l.parameters(), "lr" : args.learning_rate * ext_dist.my_size}
            ], lr=args.learning_rate)
            
    ### main loop ###
    def time_wrap(use_gpu):
        if use_gpu:
            torch.cuda.synchronize()
        return time.time()

    def dlrm_wrap(X, lS_o, lS_i, use_gpu, device):
        if use_gpu:  # .cuda()
            # lS_i can be either a list of tensors or a stacked tensor.
            # Handle each case below:
            lS_i = [S_i.to(device) for S_i in lS_i] if isinstance(lS_i, list) \
                else lS_i.to(device)
            lS_o = [S_o.to(device) for S_o in lS_o] if isinstance(lS_o, list) \
                else lS_o.to(device)
            return dlrm(
                X.to(device),
                lS_o,
                lS_i
            )
        else:
            return dlrm(X, lS_o, lS_i)

    def loss_fn_wrap(Z, T, use_gpu, device):
        if args.loss_function == "mse" or args.loss_function == "bce":
            if use_gpu:
                return loss_fn(Z, T.to(device))
            else:
                return loss_fn(Z, T)
        elif args.loss_function == "wbce":
            if use_gpu:
                loss_ws_ = loss_ws[T.data.view(-1).long()].view_as(T).to(device)
                loss_fn_ = loss_fn(Z, T.to(device))
            else:
                loss_ws_ = loss_ws[T.data.view(-1).long()].view_as(T)
                loss_fn_ = loss_fn(Z, T.to(device))
            loss_sc_ = loss_ws_ * loss_fn_
            # debug prints
            # print(loss_ws_)
            # print(loss_fn_)
            return loss_sc_.mean()

    # training or inference
    best_gA_test = 0
    best_auc_test = 0
    skip_upto_epoch = 0
    skip_upto_batch = 0
    total_time = 0
    total_loss = 0
    total_accu = 0
    total_iter = 0
    total_samp = 0
    k = 0

    # Load model is specified
    if not (args.load_model == ""):
        print("Loading saved model {}".format(args.load_model))
        if use_gpu:
            if dlrm.ndevices > 1:
                # NOTE: when targeting inference on multiple GPUs,
                # load the model as is on CPU or GPU, with the move
                # to multiple GPUs to be done in parallel_forward
                ld_model = torch.load(args.load_model)
            else:
                # NOTE: when targeting inference on single GPU,
                # note that the call to .to(device) has already happened
                ld_model = torch.load(
                    args.load_model,
                    map_location=torch.device('cuda')
                    # map_location=lambda storage, loc: storage.cuda(0)
                )
        else:
            # when targeting inference on CPU
            ld_model = torch.load(args.load_model, map_location=torch.device('cpu'))
        dlrm.load_state_dict(ld_model["state_dict"])
        ld_j = ld_model["iter"]
        ld_k = ld_model["epoch"]
        ld_nepochs = ld_model["nepochs"]
        ld_nbatches = ld_model["nbatches"]
        ld_nbatches_test = ld_model["nbatches_test"]
        ld_gA = ld_model["train_acc"]
        ld_gL = ld_model["train_loss"]
        ld_total_loss = ld_model["total_loss"]
        ld_total_accu = ld_model["total_accu"]
        ld_gA_test = ld_model["test_acc"]
        ld_gL_test = ld_model["test_loss"]
        if not args.inference_only:
            optimizer.load_state_dict(ld_model["opt_state_dict"])
            best_gA_test = ld_gA_test
            total_loss = ld_total_loss
            total_accu = ld_total_accu
            skip_upto_epoch = ld_k  # epochs
            skip_upto_batch = ld_j  # batches
        else:
            args.print_freq = ld_nbatches
            args.test_freq = 0

        print(
            "Saved at: epoch = {:d}/{:d}, batch = {:d}/{:d}, ntbatch = {:d}".format(
                ld_k, ld_nepochs, ld_j, ld_nbatches, ld_nbatches_test
            )
        )
        print(
            "Training state: loss = {:.6f}, accuracy = {:3.3f} %".format(
                ld_gL, ld_gA * 100
            )
        )
        print(
            "Testing state: loss = {:.6f}, accuracy = {:3.3f} %".format(
                ld_gL_test, ld_gA_test * 100
            )
        )

    ext_dist.barrier()
    startTime = time.time()
    startTime0 = startTime
    skipped = 0        




    #####################################################################################################################################
    #####################################################################################################################################
    #####################################################################################################################################
    #####################################################################################################################################
    #####################################################################################################################################
    #CONVERT TO NEW FORMAT HERE!#########################################################################################################

    #currently, the embedding tables are concatenated inside the inference loop, which slows it down. Move it to here.



    print("time/loss/accuracy (if enabled):")
    with torch.autograd.profiler.profile(args.enable_profiling, use_cuda=use_gpu, record_shapes=True) as prof:
        while k < args.nepochs:
            if k < skip_upto_epoch:
                continue

            accum_time_begin = time_wrap(use_gpu)

            if args.mlperf_logging:
                previous_iteration_time = None

            #regarding enumerate(train_ld which is a dataloader)  https://pytorch.org/docs/stable/data.html

            for j, (X, lS_o, lS_i, T) in enumerate(train_ld):
                if j == 0 and args.save_onnx:
                    (X_onnx, lS_o_onnx, lS_i_onnx) = (X, lS_o, lS_i)

                if j < skip_upto_batch:
                    continue

                if (skipped == 2):
                    ext_dist.barrier()
                    startTime = time.time()
                    ext_dist.orig_print("ORIG TIME: ", startTime, accum_time_begin, startTime - accum_time_begin, " for process ", ext_dist.my_rank)
                skipped = skipped + 1                

                if args.mlperf_logging:
                    current_time = time_wrap(use_gpu)
                    if previous_iteration_time:
                        iteration_time = current_time - previous_iteration_time
                    else:
                        iteration_time = 0
                    previous_iteration_time = current_time
                else:
                    t1 = time_wrap(use_gpu)

                # early exit if nbatches was set by the user and has been exceeded
                if nbatches > 0 and j >= nbatches:
                    break
                '''
                # debug prints
                print("input and targets")
                print(X.detach().cpu().numpy())
                print([np.diff(S_o.detach().cpu().tolist()
                       + list(lS_i[i].shape)).tolist() for i, S_o in enumerate(lS_o)])
                print([S_i.detach().cpu().numpy().tolist() for S_i in lS_i])
                print(T.detach().cpu().numpy())
                '''
                # Skip the batch if batch size not multiple of total ranks
                if ext_dist.my_size > 1 and X.size(0) % ext_dist.my_size != 0:
                    print("Warning: Skiping the batch %d with size %d" % (j, X.size(0)))
                    continue                

                #####################################################################################################################################
                #####################################################################################################################################
                #####################################################################################################################################
                #####################################################################################################################################                
                # forward pass
                Z = dlrm_wrap(X, lS_o, lS_i, use_gpu, device)

                # loss
                E = loss_fn_wrap(Z, T, use_gpu, device)
                '''
                # debug prints
                print("output and loss")
                print(Z.detach().cpu().numpy())
                print(E.detach().cpu().numpy())
                '''
                # compute loss and accuracy
                L = E.detach().cpu().numpy()  # numpy array
                S = Z.detach().cpu().numpy()  # numpy array
                T = T.detach().cpu().numpy()  # numpy array
                mbs = T.shape[0]  # = args.mini_batch_size except maybe for last
                A = np.sum((np.round(S, 0) == T).astype(np.uint8))

                if not args.inference_only:
                    # scaled error gradient propagation
                    # (where we do not accumulate gradients across mini-batches)
                    optimizer.zero_grad()
                    # backward pass
                    E.backward()
                    # debug prints (check gradient norm)
                    # for l in mlp.layers:
                    #     if hasattr(l, 'weight'):
                    #          print(l.weight.grad.norm().item())

                    # optimizer
                    optimizer.step()
                    ### lr_scheduler.step()

                if args.mlperf_logging:
                    total_time += iteration_time
                else:
                    t2 = time_wrap(use_gpu)
                    total_time += t2 - t1
                total_accu += A
                total_loss += L * mbs
                total_iter += 1
                total_samp += mbs

                should_print = ((j + 1) % args.print_freq == 0) or (j + 1 == nbatches)
                should_test = (
                    (args.test_freq > 0)
                    and (args.data_generation == "dataset")
                    and (((j + 1) % args.test_freq == 0) or (j + 1 == nbatches))
                )

                # print time, loss and accuracy
                if should_print or should_test:
                    gT = 1000.0 * total_time / total_iter if args.print_time else -1
                    total_time = 0

                    gA = total_accu / total_samp
                    total_accu = 0

                    gL = total_loss / total_samp
                    total_loss = 0

                    str_run_type = "inference" if args.inference_only else "training"
                    print(
                        "Finished {} it {}/{} of epoch {}, {:.2f} ms/it, ".format(
                            str_run_type, j + 1, nbatches, k, gT
                        )
                        + "loss {:.6f}, accuracy {:3.3f} % it {} for task {} ".format(gL,
                            gA * 100, total_iter, ext_dist.my_rank)
                    )
                    # Uncomment the line below to print out the total time with overhead
                    if ext_dist.my_rank < 2:
                      tt1 = time_wrap(use_gpu)
                      ext_dist.orig_print("Accumulated time so far: {} for process {} for step {} at {}" \
                       .format(tt1 - accum_time_begin, ext_dist.my_rank, skipped, tt1))
                    total_iter = 0
                    total_samp = 0

                # testing
                if should_test and not args.inference_only:
                    # don't measure training iter time in a test iteration
                    if args.mlperf_logging:
                        previous_iteration_time = None

                    test_accu = 0
                    test_loss = 0
                    test_samp = 0

                    accum_test_time_begin = time_wrap(use_gpu)
                    if args.mlperf_logging:
                        scores = []
                        targets = []

                    for i, (X_test, lS_o_test, lS_i_test, T_test) in enumerate(test_ld):
                        # early exit if nbatches was set by the user and was exceeded
                        if nbatches > 0 and i >= nbatches:
                            break

                        # Skip the batch if batch size not multiple of total ranks
                        if ext_dist.my_size > 1 and X_test.size(0) % ext_dist.my_size != 0:
                            print("Warning: Skiping the batch %d with size %d" % (i, X_test.size(0)))
                            continue

                        t1_test = time_wrap(use_gpu)

                        # forward pass
                        Z_test = dlrm_wrap(
                            X_test, lS_o_test, lS_i_test, use_gpu, device
                        )                                                        

                        if args.mlperf_logging:
                            S_test = Z_test.detach().cpu().numpy()  # numpy array
                            T_test = T_test.detach().cpu().numpy()  # numpy array
                            scores.append(S_test)
                            targets.append(T_test)
                        else:
                            # loss
                            E_test = loss_fn_wrap(Z_test, T_test, use_gpu, device)

                            # compute loss and accuracy
                            L_test = E_test.detach().cpu().numpy()  # numpy array
                            S_test = Z_test.detach().cpu().numpy()  # numpy array
                            T_test = T_test.detach().cpu().numpy()  # numpy array
                            mbs_test = T_test.shape[0]  # = mini_batch_size except last
                            A_test = np.sum((np.round(S_test, 0) == T_test).astype(np.uint8))
                            test_accu += A_test
                            test_loss += L_test * mbs_test
                            test_samp += mbs_test

                        t2_test = time_wrap(use_gpu)

                    if args.mlperf_logging:
                        scores = np.concatenate(scores, axis=0)
                        targets = np.concatenate(targets, axis=0)

                        metrics = {
                            'loss' : sklearn.metrics.log_loss,
                            'recall' : lambda y_true, y_score:
                            sklearn.metrics.recall_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'precision' : lambda y_true, y_score:
                            sklearn.metrics.precision_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'f1' : lambda y_true, y_score:
                            sklearn.metrics.f1_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            'ap' : sklearn.metrics.average_precision_score,
                            'roc_auc' : sklearn.metrics.roc_auc_score,
                            'accuracy' : lambda y_true, y_score:
                            sklearn.metrics.accuracy_score(
                                y_true=y_true,
                                y_pred=np.round(y_score)
                            ),
                            # 'pre_curve' : sklearn.metrics.precision_recall_curve,
                            # 'roc_curve' :  sklearn.metrics.roc_curve,
                        }

                        # print("Compute time for validation metric : ", end="")
                        # first_it = True
                        validation_results = {}
                        for metric_name, metric_function in metrics.items():
                            # if first_it:
                            #     first_it = False
                            # else:
                            #     print(", ", end="")
                            # metric_compute_start = time_wrap(False)
                            validation_results[metric_name] = metric_function(
                                targets,
                                scores
                            )
                            # metric_compute_end = time_wrap(False)
                            # met_time = metric_compute_end - metric_compute_start
                            # print("{} {:.4f}".format(metric_name, 1000 * (met_time)),
                            #      end="")
                        # print(" ms")
                        gA_test = validation_results['accuracy']
                        gL_test = validation_results['loss']
                    else:
                        gA_test = test_accu / test_samp
                        gL_test = test_loss / test_samp

                    is_best = gA_test > best_gA_test
                    if is_best:
                        best_gA_test = gA_test
                        if not (args.save_model == ""):
                            print("Saving model to {}".format(args.save_model))
                            torch.save(
                                {
                                    "epoch": k,
                                    "nepochs": args.nepochs,
                                    "nbatches": nbatches,
                                    "nbatches_test": nbatches_test,
                                    "iter": j + 1,
                                    "state_dict": dlrm.state_dict(),
                                    "train_acc": gA,
                                    "train_loss": gL,
                                    "test_acc": gA_test,
                                    "test_loss": gL_test,
                                    "total_loss": total_loss,
                                    "total_accu": total_accu,
                                    "opt_state_dict": optimizer.state_dict(),
                                },
                                args.save_model,
                            )

                    if args.mlperf_logging:
                        is_best = validation_results['roc_auc'] > best_auc_test
                        if is_best:
                            best_auc_test = validation_results['roc_auc']

                        print(
                            "Testing at - {}/{} of epoch {},".format(j + 1, nbatches, k)
                            + " loss {:.6f}, recall {:.4f}, precision {:.4f},".format(
                                validation_results['loss'],
                                validation_results['recall'],
                                validation_results['precision']
                            )
                            + " f1 {:.4f}, ap {:.4f},".format(
                                validation_results['f1'],
                                validation_results['ap'],
                            )
                            + " auc {:.4f}, best auc {:.4f},".format(
                                validation_results['roc_auc'],
                                best_auc_test
                            )
                            + " accuracy {:3.3f} %, best accuracy {:3.3f} %".format(
                                validation_results['accuracy'] * 100,
                                best_gA_test * 100
                            )
                        )
                    else:
                        print(
                            "Testing at - {}/{} of epoch {},".format(j + 1, nbatches, 0)
                            + " loss {:.6f}, accuracy {:3.3f} %, best {:3.3f} %".format(
                                gL_test, gA_test * 100, best_gA_test * 100
                            )
                        )
                    # Uncomment the line below to print out the total time with overhead
                    # print("Total test time for this group: {}" \
                    # .format(time_wrap(use_gpu) - accum_test_time_begin))

                    if (args.mlperf_logging
                        and (args.mlperf_acc_threshold > 0)
                        and (best_gA_test > args.mlperf_acc_threshold)):
                        print("MLPerf testing accuracy threshold "
                              + str(args.mlperf_acc_threshold)
                              + " reached, stop training")
                        break

                    if (args.mlperf_logging
                        and (args.mlperf_auc_threshold > 0)
                        and (best_auc_test > args.mlperf_auc_threshold)):
                        print("MLPerf testing auc threshold "
                              + str(args.mlperf_auc_threshold)
                              + " reached, stop training")
                        break

                #if (ext_dist.my_rank == 0 and should_print):
                #    print("ITER : ", j, " from nvidia-smi")
                #    os.system("nvidia-smi")

            k += 1  # nepochs

    #if (ext_dist.my_rank == 0):
    #    # print(torch.cuda.memory_allocated(0))
    #    print(torch.cuda.memory_summary(0))
    #    # print("from nvidia-smi")
    #    os.system("nvidia-smi")

    tt2 = time.time()
    endTime = tt2 - startTime
    ext_dist.barrier()
    tt3 = time.time()
    finalTime = tt3 - startTime
    if (skipped > 2):
        skipped -= 2
    ext_dist.orig_print("Process {} Done with total time {:.6f} measure time {:.6f}s {:.6f}s, \
        iter {:.1f}ms {:.1f}ms steps {} {}".format(ext_dist.my_rank, tt3 - startTime0,
        finalTime, endTime, finalTime*1000.0/skipped, endTime*1000.0/skipped, skipped, tt2), flush=True)

    file_prefix = "%s/dlrm_s_pytorch_r%d" % (args.out_dir, ext_dist.my_rank)
    # profiling
    if args.enable_profiling:
        os.makedirs(args.out_dir, exist_ok=True)
        with open("TT"+str(uuid.uuid4().hex), "w") as prof_f:
            prof_f.write(prof.key_averages(group_by_input_shape=True).table(
                sort_by="self_cpu_time_total",
            ))

#        with open("%s.prof" % file_prefix, "w") as prof_f:
#            prof_f.write(prof.key_averages().table(sort_by="cpu_time_total"))
#            prof.export_chrome_trace("./%s.json" % file_prefix)
#            print(prof.key_averages().table(sort_by="cpu_time_total"))

    # plot compute graph
    if args.plot_compute_graph:
        sys.exit(
            "ERROR: Please install pytorchviz package in order to use the"
            + " visualization. Then, uncomment its import above as well as"
            + " three lines below and run the code again."
        )
        # os.makedirs(args.out_dir, exist_ok=True)
        # V = Z.mean() if args.inference_only else E
        # dot = make_dot(V, params=dict(dlrm.named_parameters()))
        # dot.render('%s_graph' % file_prefix) # write .pdf file

    # test prints
    if not args.inference_only and args.debug_mode:
        print("updated parameters (weights and bias):")
        for param in dlrm.parameters():
            print(param.detach().cpu().numpy())

    # export the model in onnx
    if args.save_onnx:

        dlrm_pytorch_onnx_file = "dlrm_s_pytorch.onnx"
        torch.onnx.export(
            dlrm, (X_onnx, lS_o_onnx, lS_i_onnx), dlrm_pytorch_onnx_file, verbose=True, use_external_data_format=True
        )

        # recover the model back
        dlrm_pytorch_onnx = onnx.load("%s.onnx" % file_prefix)
        # check the onnx model
        onnx.checker.check_model(dlrm_pytorch_onnx)
