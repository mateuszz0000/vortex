import unittest
import torch

from vortex.networks.modules.backbones.darknet53 import Darknet53

class Darknet53Test(unittest.TestCase) :
    def test_forward(self) :
        darknet53 = Darknet53()
        results = darknet53(torch.rand(1,3,256,256))
        self.assertEqual(
            results.size(),
            torch.Size([1,1000])
        )