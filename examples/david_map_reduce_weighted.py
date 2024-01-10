# © Crown Copyright GCHQ
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Example coreset generation using an image of the statue of David.

This example showcases how a coreset can be generated from image data. In this context,
a coreset is a set of pixels that best capture the information in the original image.

The coreset is generated using scalable Stein kernel herding, with a PCIMQ base kernel.
The score function (gradient of the log-density function) for the Stein kernel is
estimated by applying kernel density estimation (KDE) to the data, and then taking
gradients.

The coreset attained from Stein kernel herding is compared to a coreset generated via
uniform random sampling. Coreset quality is measured using maximum mean discrepancy
(MMD).
"""

# Support annotations with | in Python < 3.10
# TODO: Remove once no longer supporting old code
from __future__ import annotations

from pathlib import Path

import cv2
import jax.numpy as jnp
import matplotlib.pyplot as plt
import numpy as np

from coreax.coresubset import KernelHerding, RandomSample
from coreax.data import ArrayData
from coreax.kernel import (
    PCIMQKernel,
    SquaredExponentialKernel,
    SteinKernel,
    median_heuristic,
)
from coreax.metrics import MMD
from coreax.reduction import MapReduce, SizeReduce
from coreax.score_matching import KernelDensityMatching
from coreax.weights import MMD as MMDWeightsOptimiser


def main(
    in_path: Path = Path("../examples/data/david_orig.png"),
    out_path: Path | None = None,
) -> tuple[float, float]:
    """
    Run the 'david' example for image sampling.

    Take an image of the statue of David and then generate a coreset using
    scalable Stein kernel herding. Compare the result from this to a coreset generated
    via uniform random sampling. Coreset quality is measured using maximum mean
    discrepancy (MMD).

    :param in_path: Path to input image, assumed relative to this module file unless an
        absolute path is given
    :param out_path: Path to save output to, if not :data:`None`, assumed relative to
        this module file unless an absolute path is given
    :return: Coreset MMD, random sample MMD
    """
    # Convert to absolute paths
    if not in_path.is_absolute():
        in_path = Path(__file__).parent / in_path
    if out_path is not None and not out_path.is_absolute():
        out_path = Path(__file__).parent / out_path

    # Path to original image
    original_data = cv2.imread(str(in_path))
    image_data = cv2.cvtColor(original_data, cv2.COLOR_BGR2GRAY)

    print(f"Image dimensions: {image_data.shape}")
    array_data = np.column_stack(np.where(image_data < 255))
    pixel_values = image_data[image_data < 255]
    array_data = np.column_stack((array_data, pixel_values)).astype(np.float32)
    num_data_points = array_data.shape[0]

    # Request 8000 coreset points
    coreset_size = 8000

    # Setup the original data object
    data = ArrayData(original_data=array_data, pre_coreset_array=array_data)

    # Set the length_scale parameter of the kernel from at most 1000 samples
    num_samples_length_scale = min(num_data_points, 1000)
    idx = np.random.choice(num_data_points, num_samples_length_scale, replace=False)
    length_scale = median_heuristic(array_data[idx].astype(float))
    if length_scale == 0.0:
        length_scale = 100.0

    # Learn a score function via kernel density estimation (this is required for
    # evaluation of the Stein kernel)
    kernel_density_score_matcher = KernelDensityMatching(
        length_scale=length_scale, kde_data=array_data[idx, :]
    )
    score_function = kernel_density_score_matcher.match()

    # Define a kernel to use for herding
    herding_kernel = SteinKernel(
        PCIMQKernel(length_scale=length_scale),
        score_function=score_function,
    )

    # Define a weights optimiser to learn optimal weights for the coreset after creation
    weights_optimiser = MMDWeightsOptimiser(kernel=herding_kernel)

    print("Computing coreset...")
    # Compute a coreset using kernel herding with a Stein kernel. To reduce memory
    # usage, we apply MapReduce, which partitions the input into blocks for independent
    # coreset solving.
    herding_object = KernelHerding(
        kernel=herding_kernel, weights_optimiser=weights_optimiser
    )
    herding_object.fit(
        original_data=data,
        strategy=MapReduce(coreset_size=coreset_size, leaf_size=10000),
    )
    herding_weights = herding_object.solve_weights()

    print("Choosing random subset...")
    # Generate a coreset via uniform random sampling for comparison
    random_sample_object = RandomSample(unique=True)
    random_sample_object.fit(
        original_data=data,
        strategy=SizeReduce(coreset_size=coreset_size),
    )

    # Define a reference kernel to use for comparisons of MMD. We'll use a normalised
    # SquaredExponentialKernel (which is also a Gaussian kernel)
    mmd_kernel = SquaredExponentialKernel(
        length_scale=length_scale,
        output_scale=1.0 / (length_scale * jnp.sqrt(2.0 * jnp.pi)),
    )

    # Compute the MMD between the original data and the coreset generated via herding
    metric_object = MMD(kernel=mmd_kernel)
    maximum_mean_discrepancy_herding = metric_object.compute(
        data.original_data, herding_object.coreset
    )

    # Compute the MMD between the original data and the coreset generated via random
    # sampling
    maximum_mean_discrepancy_random = metric_object.compute(
        data.original_data, random_sample_object.coreset
    )

    # Print the MMD values
    print(f"Random sampling coreset MMD: {maximum_mean_discrepancy_random}")
    print(f"Herding coreset MMD: {maximum_mean_discrepancy_herding}")

    print("Plotting")
    # Plot the original image
    plt.figure(figsize=(10, 5))
    plt.subplot(1, 3, 1)
    plt.imshow(image_data, cmap="gray")
    plt.title("Original")
    plt.axis("off")

    # Plot the coreset image and weight the points using a function of the coreset
    # weights
    plt.subplot(1, 3, 2)
    plt.scatter(
        array_data[herding_object.coreset_indices, 1],
        -array_data[herding_object.coreset_indices, 0],
        c=array_data[herding_object.coreset_indices, 2],
        cmap="gray",
        s=np.exp(2.0 * coreset_size * herding_weights).reshape(1, -1),
        marker="h",
        alpha=0.8,
    )
    plt.axis("scaled")
    plt.title("Coreset")
    plt.axis("off")

    # Plot the image of randomly sampled points
    plt.subplot(1, 3, 3)
    plt.scatter(
        array_data[random_sample_object.coreset_indices, 1],
        -array_data[random_sample_object.coreset_indices, 0],
        c=array_data[random_sample_object.coreset_indices, 2],
        s=1.0,
        cmap="gray",
        marker="h",
        alpha=0.8,
    )
    plt.axis("scaled")
    plt.title("Random")
    plt.axis("off")

    if out_path is not None:
        plt.savefig(out_path)

    plt.show()

    return (
        float(maximum_mean_discrepancy_herding),
        float(maximum_mean_discrepancy_random),
    )


if __name__ == "__main__":
    main()
