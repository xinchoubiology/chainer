import numpy
from chainer import cuda, Function

def _cu_conv_sum(y, x, n):
    # Convolutional sum
    # TODO(beam2d): Use scan computation
    rdim = x.size / (x.shape[0] * x.shape[1])
    cuda.elementwise(
        'float* y, const float* x, int rdim, int N, int n_',
        '''
          int half_n = n_ / 2;
          int offset = i / rdim * N * rdim + i % rdim;
          float* xi = x + offset;
          float* yi = y + offset;

          float sum_part = 0;
          for (int j = 0; j < N + half_n; ++j) {
            if (j < N) {
              sum_part += xi[j * rdim];
            }
            if (j >= n_) {
              sum_part -= xi[(j - n_) * rdim];
            }
            if (j >= half_n) {
              yi[(j - half_n) * rdim] = sum_part;
            }
          }
        ''', 'lrn_conv_sum')(y, x, rdim, x.shape[1], n,
                             range=slice(0, x.shape[0] * rdim, 1))

class LocalResponseNormalization(Function):
    """Cross-channel normalization function used in AlexNet."""

    def __init__(self, n=5, k=2, alpha=1e-4, beta=.75):
        self.n     = n
        self.k     = k
        self.alpha = alpha
        self.beta  = beta

    def forward_cpu(self, x):
        half_n = self.n / 2
        x2 = x[0] * x[0]
        sum_part = x2.copy()
        for i in xrange(1, half_n + 1):
            sum_part[:, i:  ] += x2[:,  :-i]
            sum_part[:,  :-i] += x2[:, i:  ]
        self.unit_scale = self.k + self.alpha * sum_part
        self.scale      = self.unit_scale ** -self.beta
        self.y          = x[0] * self.scale
        return self.y,

    def backward_cpu(self, x, gy):
        half_n = self.n / 2
        summand = self.y * gy[0] / self.unit_scale
        sum_part = summand.copy()
        for i in xrange(1, half_n + 1):
            sum_part[:, i:  ] += summand[:,  :-i]
            sum_part[:,  :-i] += summand[:, i:  ]

        gx = gy[0] * self.scale - 2 * self.alpha * self.beta * x[0] * sum_part
        return gx,

    def forward_gpu(self, x):
        self.y = x[0] * x[0]  # temporary
        self.scale = cuda.empty_like(self.y)
        _cu_conv_sum(self.scale, self.y, self.n)
        cuda.elementwise(
            '''float* y, float* scale, const float* x,
               float k, float alpha, float beta''',
            '''scale[i] = k + alpha * scale[i];
               y[i] = x[i] * __powf(scale[i], -beta);''',
            'lrn_fwd')(self.y, self.scale, x[0], self.k, self.alpha, self.beta)
        return self.y,

    def backward_gpu(self, x, gy):
        summand = cuda.empty_like(x[0])
        cuda.elementwise(
            '''float* summand, const float* scale, const float* y,
               const float* gy''',
            'summand[i] = y[i] * gy[i] / scale[i]',
            'lrn_bwd_summand')(summand, self.scale, self.y, gy[0])
        gx = cuda.empty_like(x[0])
        _cu_conv_sum(gx, summand, self.n)
        cuda.elementwise(
            '''float* gx, const float* x, const float* gy, const float* scale,
               float beta, float coeff''',
            'gx[i] = __powf(scale[i], -beta) * gy[i] - coeff * x[i] * gx[i]',
            'lrn_bwd')(gx, x[0], gy[0], self.scale, self.beta,
                       2 * self.alpha * self.beta)
        return gx,


def local_response_normalization(x, n=5, k=2, alpha=1e-4, beta=.75):
    """Local response normalization across neighboring channels.

    This function implements normalization across channels. Let :math:`x` an
    input image with :math:`N` channels. Then, this function computes an output
    image :math:`y` by following formula:

    .. math::
       y_i = {x_i \\over \\left( k + \\
              \\alpha \\sum_{j=\\max{1, i - n/2}}^{\\min{N, i + n/2}} \\
              x_j^2 \\right)^\\beta}.

    Args:
        x (Variable): Input variable.
        n (int): Normalization window width.
        k (float): Smoothing parameter.
        alpha (float): Normalizer scaling parameter.
        beta (float): Normalizer power parameter.

    Returns:
        Variable: Output variable.

    See: SSec. 3.3 of `ImageNet Classification with Deep Convolutional Neural \\
    Networks <http://www.cs.toronto.edu/~fritz/absps/imagenet.pdf>`_

    """
    return LocalResponseNormalization(n, k, alpha, beta)(x)
