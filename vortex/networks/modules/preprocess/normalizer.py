import torch
import torchvision
import torch.nn as nn
from typing import Type, List, Tuple, Dict, Union
import numpy as np

def to_tensor(img: torch.Tensor,scaler : int = 255):
    """Convert a torch.ByteTensor with range of value of [0,255] and NHWC layout to 
    torch.FloatTensor with NCHW layout with scaling option
    Args:
        torch.ByteTensor
        scaler
    Returns:
        Tensor: Converted image.
    """

    assert len(img.size()) == 4, "to_tensor only support 4-dimension tensor (NHWC) as input, "\
        "got size of %s" % list(img.size())
    assert img.size(3) == 3 or img.size(3) == 1, "to_tensor only support NHWC input layout, "\
        "got size of %s" % list(img.size())

    # img = img.transpose(0, 1).transpose(0, 2)
    img = img.permute(0, 3, 1, 2)
    img = img.float().div(scaler)
    return img


def normalize(tensor, mean, std):
    """Normalize a tensor image with mean and standard deviation.

    .. note::
        This transform acts out of place by default, i.e., it does not mutates the input tensor.

    See :class:`~torchvision.transforms.Normalize` for more details.

    Args:
        tensor (Tensor): Tensor image of size (C, H, W) to be normalized.
        mean (sequence): Sequence of means for each channel.
        std (sequence): Sequence of standard deviations for each channel.
        inplace(bool,optional): Bool to make this operation inplace.

    Returns:
        Tensor: Normalized Tensor image.
    """
    if isinstance(mean,list) or isinstance(mean,tuple):
        mean=torch.as_tensor(mean, dtype=torch.float)
    if isinstance(std,list) or isinstance(std,tuple):
        std=torch.as_tensor(std, dtype=torch.float)
    # dtype = tensor.dtype
    # mean = torch.as_tensor(mean, dtype=dtype, device=tensor.device)
    # std = torch.as_tensor(std, dtype=dtype, device=tensor.device)
    # tensor.sub_(mean[:, None, None]).div_(std[:, None, None])
    tensor.sub_(mean[None, :, None, None]).div_(std[None, :, None, None])
    return tensor


class Normalize(nn.Module):
    """ Exportable input tensor normalizer.
    """
    __constants__ = ["mean", "std","scaler"]

    def __init__(self, mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225], scaler = 255):
        super(Normalize, self).__init__()
        self.register_buffer("mean", torch.as_tensor(mean, dtype=torch.float))
        self.register_buffer("std", torch.as_tensor(std, dtype=torch.float))
        self.register_buffer("scaler", torch.as_tensor(scaler, dtype=torch.int))

    def forward(self, input: torch.Tensor) -> torch.Tensor:
        x = to_tensor(input,self.scaler)
        x = normalize(x, self.mean, self.std)
        return x


def get_preprocess(*args, **kwargs):
    """ 
    :type *args:
    :param *args: postional arguments to be forwarded to `Normalize`

    :type **kwargs:
    :param **kwargs: keyword arguments to be forwarded to `Normalize`

    :raises:

    :rtype: Normalize
    """
    return Normalize(*args, **kwargs)
