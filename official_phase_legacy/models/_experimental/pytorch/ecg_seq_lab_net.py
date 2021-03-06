"""
Sequence labeling nets, for wave delineation,

the labeling granularity is the frequency of the input signal,
divided by the length (counted by the number of basic blocks) of each branch

pipeline:
multi-scopic cnn --> (bidi-lstm -->) "attention" --> seq linear
"""
from copy import deepcopy
from itertools import repeat
from collections import OrderedDict
from typing import Union, Optional, Tuple, Sequence, NoReturn
from numbers import Real, Number

import numpy as np
np.set_printoptions(precision=5, suppress=True)
import pandas as pd
import torch
from torch import nn
from torch import Tensor
import torch.nn.functional as F
from easydict import EasyDict as ED

from cfg import ModelCfg
# from model_configs import ECG_CRNN_CONFIG
from models.utils.torch_utils import (
    Mish, Swish, Activations,
    Bn_Activation, Conv_Bn_Activation,
    MultiConv,
    DownSample,
    StackedLSTM,
    AttentivePooling,
    SeqLin,
)
from utils.utils_nn import compute_conv_output_shape
from utils.misc import dict_to_str

if ModelCfg.torch_dtype.lower() == 'double':
    torch.set_default_tensor_type(torch.DoubleTensor)


class MultiScopicBasicBlock(nn.Sequential):
    """
    basic building block of the CNN part of the SOTA model from CPSC2019 challenge (entry 0416)

    (conv -> activation) * N --> bn --> down_sample
    """
    __DEBUG__ = True
    __name__ = "MultiScopicBasicBlock"

    def __init__(self, in_channels:int, scopes:Sequence[int], num_filters:Union[int,Sequence[int]], filter_lengths:Union[int,Sequence[int]], subsample_length:int, groups:int=1, **config) -> NoReturn:
        """ finished, not checked,

        Parameters:
        -----------
        in_channels: int,
            number of channels in the input
        scopes: sequence of int,
            scopes of the convolutional layers, via `dilation`
        num_filters: int or sequence of int,
        filter_lengths: int or sequence of int,
        subsample_length: int,
        """
        super().__init__()
        self.__in_channels = in_channels
        self.__scopes = scopes
        self.__num_convs = len(self.__scopes)
        if isinstance(num_filters, int):
            self.__out_channels = list(repeat(num_filters, self.__num_convs))
        else:
            self.__out_channels = num_filters
            assert len(self.__out_channels) == self.__num_convs, \
                f"`scopes` indicates {self.__num_convs} convolutional layers, while `num_filters` indicates {len(self.__out_channels)}"
        if isinstance(filter_lengths, int):
            self.__filter_lengths = list(repeat(filter_lengths, self.__num_convs))
        else:
            self.__filter_lengths = filter_lengths
            assert len(self.__filter_lengths) == self.__num_convs, \
                f"`scopes` indicates {self.__num_convs} convolutional layers, while `filter_lengths` indicates {len(self.__filter_lengths)}"
        self.__subsample_length = subsample_length
        self.__groups = groups
        self.config = ED(deepcopy(config))

        conv_in_channels = self.__in_channels
        for idx in range(self.__num_convs):
            self.add_module(
                f"ca_{idx}",
                Conv_Bn_Activation(
                    in_channels=conv_in_channels,
                    out_channels=self.__out_channels[idx],
                    kernel_size=self.__filter_lengths[idx],
                    stride=1,
                    dilation=self.__scopes[idx],
                    groups=self.__groups,
                    batch_norm=False,
                    activation=self.config.activation,
                    kw_activation=self.config.kw_activation,
                    kernel_initializer=self.config.kernel_initializer,
                    kw_initializer=self.config.kw_initializer,
                    bias=self.config.bias,
                )
            )
            conv_in_channels = self.__out_channels[idx]
        self.add_module(
            "bn",
            nn.BatchNorm1d(self.__out_channels[-1])
        )
        self.add_module(
            "down",
            DownSample(
                down_scale=self.__subsample_length,
                in_channels=self.__out_channels[-1],
                groups=self.__groups,
                # padding=
                batch_norm=False,
                mode=self.config.subsample_mode,
            )
        )
        if self.config.dropout > 0:
            self.add_module(
                "dropout",
                nn.Dropout(self.config.dropout, inplace=False)
            )

    def forward(self, input:Tensor) -> Tensor:
        """
        input: of shape (batch_size, channels, seq_len)
        """
        output = super().forward(input)
        return output

    def compute_output_shape(self, seq_len:int, batch_size:Optional[int]=None) -> Sequence[Union[int, type(None)]]:
        """ finished, checked,

        Parameters:
        -----------
        seq_len: int,
            length of the 1d sequence
        batch_size: int, optional,
            the batch size, can be None

        Returns:
        --------
        output_shape: sequence,
            the output shape of this block, given `seq_len` and `batch_size`
        """
        _seq_len = seq_len
        for idx, module in enumerate(self):
            if idx == self.__num_convs:  # bn layer
                continue
            elif self.config.dropout > 0 and idx == len(self)-1:  # dropout layer
                continue
            output_shape = module.compute_output_shape(_seq_len, batch_size)
            _, _, _seq_len = output_shape
        return output_shape

    @property
    def module_size(self):
        """
        """
        module_parameters = filter(lambda p: p.requires_grad, self.parameters())
        n_params = sum([np.prod(p.size()) for p in module_parameters])
        return n_params


class MultiScopicBranch(nn.Sequential):
    """
    branch path of the CNN part of the SOTA model from CPSC2019 challenge (entry 0416)
    """
    __DEBUG__ = True
    __name__ = "MultiScopicBranch"

    def __init__(self, in_channels:int, scopes:Sequence[Sequence[int]], num_filters:Union[Sequence[int],Sequence[Sequence[int]]], filter_lengths:Union[Sequence[int],Sequence[Sequence[int]]], subsample_lengths:Union[int,Sequence[int]], groups:int=1, **config) -> NoReturn:
        """

        Parameters:
        -----------
        in_channels
        """
        super().__init__()
        self.__in_channels = in_channels
        self.__scopes = scopes
        self.__num_blocks = len(self.__scopes)
        self.__num_filters = num_filters
        assert len(self.__num_filters) == self.__num_blocks, \
            f"`scopes` indicates {self.__num_blocks} `MultiScopicBasicBlock`s, while `num_filters` indicates {len(self.__num_filters)}"
        self.__filter_lengths = filter_lengths
        assert len(self.__filter_lengths) == self.__num_blocks, \
            f"`scopes` indicates {self.__num_blocks} `MultiScopicBasicBlock`s, while `filter_lengths` indicates {llen(self.__filter_lengths)}"
        if isinstance(subsample_lengths, int):
            self.__subsample_lengths = list(repeat(subsample_lengths, self.__num_blocks))
        else:
            self.__subsample_lengths = filter_lengths
            assert len(self.__subsample_lengths) == self.__num_blocks, \
            f"`scopes` indicates {self.__num_blocks} `MultiScopicBasicBlock`s, while `subsample_lengths` indicates {llen(self.__subsample_lengths)}"
        self.__groups = groups
        self.config = ED(deepcopy(config))

        block_in_channels = self.__in_channels
        for idx in range(self.__num_blocks):
            self.add_module(
                f"block_{idx}",
                MultiScopicBasicBlock(
                    in_channels=block_in_channels,
                    scopes=self.__scopes[idx],
                    num_filters=self.__num_filters[idx],
                    filter_lengths=self.__filter_lengths[idx],
                    subsample_length=self.__subsample_lengths[idx],
                    groups=self.__groups,
                    dropout=self.config.dropouts[idx],
                    **(self.config.block)
                )
            )
            block_in_channels = self.__num_filters[idx]

    def forward(self, input:Tensor) -> Tensor:
        """
        input: of shape (batch_size, channels, seq_len)
        """
        output = super().forward(input)
        return output

    def compute_output_shape(self, seq_len:int, batch_size:Optional[int]=None) -> Sequence[Union[int, type(None)]]:
        """ finished, checked,

        Parameters:
        -----------
        seq_len: int,
            length of the 1d sequence
        batch_size: int, optional,
            the batch size, can be None

        Returns:
        --------
        output_shape: sequence,
            the output shape of this block, given `seq_len` and `batch_size`
        """
        _seq_len = seq_len
        for idx, module in enumerate(self):
            output_shape = module.compute_output_shape(_seq_len, batch_size)
            _, _, _seq_len = output_shape
        return output_shape

    @property
    def module_size(self):
        """
        """
        module_parameters = filter(lambda p: p.requires_grad, self.parameters())
        n_params = sum([np.prod(p.size()) for p in module_parameters])
        return n_params


class MultiScopicCNN(nn.Module):
    """
    CNN part of the SOTA model from CPSC2019 challenge (entry 0416)
    """
    __DEBUG__ = True
    __name__ = "MultiScopicCNN"

    def __init__(self, in_channels:int, **config) -> NoReturn:
        """
        """
        super().__init__()
        self.__in_channels = in_channels
        self.config = ED(deepcopy(config))
        self.__scopes = self.config.scopes
        self.__num_branches = len(self.__scopes)

        if self.__DEBUG__:
            print(f"configuration of {self.__name__} is as follows\n{dict_to_str(self.config)}")

        self.branches = nn.ModuleDict()
        for idx in range(self.__num_branches):
            self.branches[f"branch_{idx}"] = \
                MultiScopicBranch(
                    in_channels=self.__in_channels,
                    scopes=self.__scopes[idx],
                    num_filters=self.config.num_filters[idx],
                    filter_lengths=self.config.filter_lengths[idx],
                    subsample_lengths=self.config.subsample_lengths[idx],
                    dropouts=self.config.dropouts[idx],
                    block=self.config.block,  # a dict
                )

    def forward(self, input:Tensor) -> Tensor:
        """
        input: of shape (batch_size, channels, seq_len)
        """
        branch_out = OrderedDict()
        for idx in range(self.__num_branches):
            key = f"branch_{idx}"
            branch_out[key] = self.branches[key].forward(input)
        output = torch.cat(
            [branch_out[f"branch_{idx}"] for idx in range(self.__num_branches)],
            dim=1,  # along channels
        )
        return output
    
    def compute_output_shape(self, seq_len:int, batch_size:Optional[int]=None) -> Sequence[Union[int, type(None)]]:
        """ finished, checked,

        Parameters:
        -----------
        seq_len: int,
            length of the 1d sequence
        batch_size: int, optional,
            the batch size, can be None

        Returns:
        --------
        output_shape: sequence,
            the output shape of this block, given `seq_len` and `batch_size`
        """
        out_channels = 0
        for idx in range(self.__num_branches):
            key = f"branch_{idx}"
            _, _branch_oc, _seq_len = \
                self.branches[key].compute_output_shape(seq_len, batch_size)
            out_channels += _branch_oc
        return (batch_size, out_channels, _seq_len)

    @property
    def module_size(self):
        """
        """
        module_parameters = filter(lambda p: p.requires_grad, self.parameters())
        n_params = sum([np.prod(p.size()) for p in module_parameters])
        return n_params


# class SeqLabAttn(nn.Module):
#     """
#     """
#     __DEBUG__ = True
#     __name__ = "SeqLabAttn"

#     def __init__(self, in_channels:int, **config) -> NoReturn:
#         """
#         """
#         self.__in_channels = in_channels
#         self.config = ED(deepcopy(config))
#         if self.__DEBUG__:
#             print(f"configuration of {self.__name__} is as follows\n{dict_to_str(self.config)}")
  
#     def forward(self, input:Tensor) -> Tensor:
#         """
#         """
#         raise NotImplementedError

#     def compute_output_shape(self, seq_len:int, batch_size:Optional[int]=None) -> Sequence[Union[int, type(None)]]:
#         """ finished, checked,

#         Parameters:
#         -----------
#         seq_len: int,
#             length of the 1d sequence
#         batch_size: int, optional,
#             the batch size, can be None

#         Returns:
#         --------
#         output_shape: sequence,
#             the output shape of this block, given `seq_len` and `batch_size`
#         """
#         raise NotImplementedError

    # @property
    # def module_size(self):
    #     """
    #     """
    #     module_parameters = filter(lambda p: p.requires_grad, self.parameters())
    #     n_params = sum([np.prod(p.size()) for p in module_parameters])
    #     return n_params


class ECG_SEQ_LAB_NET(nn.Module):
    """ NOT finished,

    SOTA model from CPSC2019 challenge (entry 0416)

    pipeline:
    multi-scopic cnn --> (bidi-lstm -->) "attention" --> seq linear
    """
    __DEBUG__ = True
    __name__ = "ECG_SEQ_LAB_NET"

    def __init__(self, classes:Sequence[str], config:dict) -> NoReturn:
        """ finished, checked,

        Parameters:
        -----------
        classes: list,
            list of the classes for sequence labeling
        config: dict, optional,
            other hyper-parameters, including kernel sizes, etc.
            ref. the corresponding config file
        """
        super().__init__()
        self.classes = list(classes)
        self.n_classes = len(classes)
        self.n_leads = 12
        self.config = ED(deepcopy(config))
        if self.__DEBUG__:
            print(f"classes (totally {self.n_classes}) for prediction:{self.classes}")
            print(f"configuration of {self.__name__} is as follows\n{dict_to_str(self.config)}")
        __debug_seq_len = 4000
        
        # currently, the CNN part only uses `MultiScopicCNN`
        # can be 'multi_scopic' or 'multi_scopic_leadwise'
        cnn_choice = self.config.cnn.name.lower()
        self.cnn = MultiScopicCNN(self.n_leads, **(self.config.cnn[cnn_choice]))
        rnn_input_size = self.cnn.compute_output_shape(__debug_seq_len, batch_size=None)[1]

        if self.__DEBUG__:
            cnn_output_shape = self.cnn.compute_output_shape(__debug_seq_len, batch_size=None)
            print(f"cnn output shape (batch_size, features, seq_len) = {cnn_output_shape}, given input seq_len = {__debug_seq_len}")
            __debug_seq_len = cnn_output_shape[-1]

        if self.config.rnn.name.lower() == 'none':
            self.rnn = None
            attn_input_size = rnn_input_size
        elif self.config.rnn.name.lower() == 'lstm':
            self.rnn = StackedLSTM(
                input_size=rnn_input_size,
                hidden_sizes=self.config.rnn.lstm.hidden_sizes,
                bias=self.config.rnn.lstm.bias,
                dropout=self.config.rnn.lstm.dropout,
                bidirectional=self.config.rnn.lstm.bidirectional,
                return_sequences=True,
            )
            attn_input_size = self.rnn.compute_output_shape(None,None)[-1]
        else:
            raise NotImplementedError

        if self.__DEBUG__:
            if self.rnn:
                rnn_output_shape = self.rnn.compute_output_shape(__debug_seq_len, batch_size=None)
            print(f"rnn output shape (seq_len, batch_size, features) = {rnn_output_shape}, given input seq_len = {__debug_seq_len}")

        self.pool = nn.AdaptiveAvgPool1d((1,))

        self.attn = nn.Sequential()
        attn_out_channels = self.config.attn.out_channels + [attn_input_size]
        self.attn.add_module(
            "attn",
            SeqLin(
                in_channels=attn_input_size,
                out_channels=attn_out_channels,
                activation=self.config.attn.activation,
                bias=self.config.attn.bias,
                kernel_initializer=self.config.attn.kernel_initializer,
                dropouts=self.config.attn.dropouts,
            )
        )
        self.attn.add_module(
            "softmax",
            nn.Softmax(-1)
        )
        
        if self.__DEBUG__:
            print(f"")

        clf_input_size = self.config.attn.out_channels[-1]
        clf_out_channels = self.config.clf.out_channels + [self.n_classes]
        self.clf = SeqLin(
            in_channels=clf_input_size,
            out_channels=clf_out_channels,
            activation=self.config.clf.activation,
            bias=self.config.clf.bias,
            kernel_initializer=self.config.clf.kernel_initializer,
            dropouts=self.config.clf.dropouts,
            skip_last_activation=True,
        )
        
        # sigmoid for inference
        self.softmax = nn.Softmax(-1)

    def forward(self, input:Tensor) -> Tensor:
        """ finished, NOT checked,
        input: of shape (batch_size, channels, seq_len)
        """
        # cnn
        cnn_output = self.cnn(input)  # (batch_size, channels, seq_len)

        # rnn or none
        if self.rnn:
            rnn_output = cnn_output.permute(2,0,1)  # (seq_len, batch_size, channels)
            rnn_output = self.rnn(rnn_output)  # (seq_len, batch_size, channels)
            rnn_output = rnn_output.permute(1,2,0)  # (batch_size, channels, seq_len)
        else:
            rnn_output = cnn_output
        x = self.pool(rnn_output)  # (batch_size, channels, 1)
        x = x.squeeze(-1)  # (batch_size, channels)

        # attention
        x = self.attn(x)  # (batch_size, channels)
        x = x.unsqueeze(-1)  # (batch_size, channels, 1)
        x = rnn_output * x  # (batch_size, channels, seq_len)
        x = x.permute(0,2,1)  # (batch_size, seq_len, channels)
        ouput = self.clf(x)
        return output

    def compute_output_shape(self, seq_len:int, batch_size:Optional[int]=None) -> Sequence[Union[int, type(None)]]:
        """ NOT finished,

        Parameters:
        -----------
        seq_len: int,
            length of the 1d sequence
        batch_size: int, optional,
            the batch size, can be None

        Returns:
        --------
        output_shape: sequence,
            the output shape of this block, given `seq_len` and `batch_size`
        """
        _seq_len = seq_len
        output_shape = self.cnn.compute_output_shape(_seq_len, batch_size)
        _, _, _seq_len = output_shape
        if self.rnn:
            output_shape = self.rnn.compute_output_shape(_seq_len, batch_size)

    @property
    def module_size(self):
        """
        """
        module_parameters = filter(lambda p: p.requires_grad, self.parameters())
        n_params = sum([np.prod(p.size()) for p in module_parameters])
        return n_params
