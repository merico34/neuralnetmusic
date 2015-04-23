"""
"""
import cPickle
import os
import sys
import time

import numpy

import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams

from DeepLearningTutorials.code.mlp import HiddenLayer
from DeepLearningTutorials.code.rbm import RBM

from PIL import Image

# compute_test_value is 'off' by default, meaning this feature is inactive
theano.config.compute_test_value = 'off' # Use 'warn' to activate this feature

# start-snippet-1
class DBN(object):
    """Deep Belief Network

    A deep belief network is obtained by stacking several RBMs on top of each
    other. The hidden layer of the RBM at layer `i` becomes the input of the
    RBM at layer `i+1`. The first layer RBM gets as input the input of the
    network, and the hidden layer of the last RBM represents the output. When
    used for classification, the DBN is treated as a MLP, by adding a logistic
    regression layer on top.
    """

    def __init__(self, numpy_rng, theano_rng=None, n_ins=784,
                 hidden_layers_sizes=[1000, 1000, 1000], n_outs=10):
        """This class is made to support a variable number of layers.

        :type numpy_rng: numpy.random.RandomState
        :param numpy_rng: numpy random number generator used to draw initial
                    weights

        :type theano_rng: theano.tensor.shared_randomstreams.RandomStreams
        :param theano_rng: Theano random generator; if None is given one is
                           generated based on a seed drawn from `rng`

        :type n_ins: int
        :param n_ins: dimension of the input to the DBN

        :type hidden_layers_sizes: list of ints
        :param hidden_layers_sizes: intermediate layers size, must contain
                               at least one value

        :type n_outs: int
        :param n_outs: dimension of the output of the network
        """

        self.sigmoid_layers = []
        self.rbm_layers = []
        self.params = []
        self.n_layers = len(hidden_layers_sizes)

        assert self.n_layers > 0

        if not theano_rng:
            theano_rng = RandomStreams(numpy_rng.randint(2 ** 30))

        # allocate symbolic variables for the data
        self.x = T.matrix('x')  # the data is presented as rasterized images

        # end-snippet-1
        # The DBN is an MLP, for which all weights of intermediate
        # layers are shared with a different RBM.  We will first
        # construct the DBN as a deep multilayer perceptron, and when
        # constructing each sigmoidal layer we also construct an RBM
        # that shares weights with that layer. During pretraining we
        # will train these RBMs (which will lead to chainging the
        # weights of the MLP as well) During finetuning we will finish
        # training the DBN by doing stochastic gradient descent on the
        # MLP.

        for i in xrange(self.n_layers):
            # construct the sigmoidal layer

            # the size of the input is either the number of hidden
            # units of the layer below or the input size if we are on
            # the first layer
            if i == 0:
                input_size = n_ins
            else:
                input_size = hidden_layers_sizes[i - 1]

            # the input to this layer is either the activation of the
            # hidden layer below or the input of the DBN if you are on
            # the first layer
            if i == 0:
                layer_input = self.x
            else:
                layer_input = self.sigmoid_layers[-1].output

            sigmoid_layer = HiddenLayer(rng=numpy_rng,
                                        input=layer_input,
                                        n_in=input_size,
                                        n_out=hidden_layers_sizes[i],
                                        activation=T.nnet.sigmoid)

            # add the layer to our list of layers
            self.sigmoid_layers.append(sigmoid_layer)


            # Construct an RBM that shared weights with this layer
            rbm_layer = RBM(numpy_rng=numpy_rng,
                            theano_rng=theano_rng,
                            input=layer_input,
                            n_visible=input_size,
                            n_hidden=hidden_layers_sizes[i],
                            W=sigmoid_layer.W,
                            hbias=sigmoid_layer.b)
            self.rbm_layers.append(rbm_layer)
            self.params.extend(rbm_layer.params)

        # And build the upside-down network.  This shares parameters with the forward network.
        # Except the weights are transposed and stuff.
        reverse_input = self.sigmoid_layers[-1].output
        self.reverse_layers = [None] * self.n_layers
        for i in reversed(xrange(self.n_layers)):    
            if i == 0:
                out_size = n_ins
            else:
                out_size = hidden_layers_sizes[i-1]
            reverse_sigmoid = HiddenLayer(rng=numpy_rng,
                input=reverse_input,
                n_in=hidden_layers_sizes[i],
                n_out=out_size,
                W=self.sigmoid_layers[i].W.T,
                b=self.rbm_layers[i].vbias,
                activation=T.nnet.sigmoid
            )
            reverse_input = reverse_sigmoid.output
            self.reverse_layers[i] = reverse_sigmoid


        # The fine-tune cost is the reconstruction error of the entire net.
        self.finetune_cost = ((self.x - self.reverse_layers[0].output)**2).sum()

    def pretraining_functions(self, train_set_x, batch_size, k):
        '''Generates a list of functions, for performing one step of
        gradient descent at a given layer. The function will require
        as input the minibatch index, and to train an RBM you just
        need to iterate, calling the corresponding function on all
        minibatch indexes.

        :type train_set_x: theano.tensor.TensorType
        :param train_set_x: Shared var. that contains all datapoints used
                            for training the RBM
        :type batch_size: int
        :param batch_size: size of a [mini]batch
        :param k: number of Gibbs steps to do in CD-k / PCD-k

        '''

        # index to a [mini]batch
        index = T.lscalar('index')  # index to a minibatch
        learning_rate = T.scalar('lr')  # learning rate to use

        # number of batches
        n_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size
        # begining of a batch, given `index`
        batch_begin = index * batch_size
        # ending of a batch given `index`
        batch_end = batch_begin + batch_size

        pretrain_fns = []
        for rbm in self.rbm_layers:

            # get the cost and the updates list
            # using CD-k here (persisent=None) for training each RBM.
            # TODO: change cost function to reconstruction error
            cost, updates = rbm.get_cost_updates(learning_rate,
                                                 persistent=None, k=k)

            # compile the theano function
            fn = theano.function(
                inputs=[index, theano.Param(learning_rate, default=0.1)],
                outputs=cost,
                updates=updates,
                givens={
                    self.x: train_set_x[batch_begin:batch_end]
                }
            )
            # append `fn` to the list of functions
            pretrain_fns.append(fn)

        return pretrain_fns

    def build_finetune_functions(self, train_set_x, batch_size, learning_rate):
        '''Generates a function `train` that implements one step of
        finetuning, a function `validate` that computes the error on a
        batch from the validation set, and a function `test` that
        computes the error on a batch from the testing set

        :type datasets: list of pairs of theano.tensor.TensorType
        :param datasets: It is a list that contain all the datasets;
                        the has to contain three pairs, `train`,
                        `valid`, `test` in this order, where each pair
                        is formed of two Theano variables, one for the
                        datapoints, the other for the labels
        :type batch_size: int
        :param batch_size: size of a minibatch
        :type learning_rate: float
        :param learning_rate: learning rate used during finetune stage

        '''

        index = T.lscalar('index')  # index to a [mini]batch
        n_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size

        # compute the gradients with respect to the model parameters
        gparams = T.grad(self.finetune_cost, self.params)

        # compute list of fine-tuning updates
        updates = []
        for param, gparam in zip(self.params, gparams):
            updates.append((param, param - gparam * learning_rate))

        train_fn = theano.function(
            inputs=[index],
            outputs=self.finetune_cost,
            updates=updates,
            givens={
                self.x: train_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
            }
        )

        test_score_i = theano.function(
            [index],
            self.finetune_cost,
            givens={
                self.x: train_set_x[
                    index * batch_size: (index + 1) * batch_size
                ],
            }
        )

        # Create a function that scans the entire test set
        def test_score():
            return [test_score_i(i) for i in xrange(n_batches)]

        return train_fn, test_score

    def generate(self, top_level):
        """
        Make a new piano roll, given top level values.
        """
        generator = theano.function(
            [],
            self.reverse_layers[0].output,
            givens={
                self.reverse_layers[-1].input: top_level
            }
        )
        return generator()

    def label(self, in_x, outer_index):
        """
        Get the top level values, given a piano roll.
        """
        idx = T.lscalar('idx')
        generator = theano.function(
            [idx],
            self.sigmoid_layers[-1].output,
            givens = {
                self.sigmoid_layers[0].input: in_x[idx:idx+10],
            }
        )
        return generator(outer_index)


def train_dbn(finetune_lr=0.01, pretraining_epochs=100,
    pretrain_lr=0.01, k=1, training_epochs=1000,
    batch_size=10):

    raw_x = cPickle.load(open('bach_data.pickle', 'rb'))
    train_set_x = theano.shared(raw_x)
    

    # compute number of minibatches for training, validation and testing
    n_train_batches = train_set_x.get_value(borrow=True).shape[0] / batch_size
    print n_train_batches

    # numpy random generator
    numpy_rng = numpy.random.RandomState()
    print '... building the model'
    if True:
        # construct the Deep Belief Network
        dbn = DBN(numpy_rng=numpy_rng, n_ins=raw_x.shape[1],
                  hidden_layers_sizes=[256, 128, 64])

        # start-snippet-2
        #########################
        # PRETRAINING THE MODEL #
        #########################
        print '... getting the pretraining functions'
        pretraining_fns = dbn.pretraining_functions(train_set_x=train_set_x,
                                                    batch_size=batch_size,
                                                    k=k)


        print '... pre-training the model'
        start_time = time.clock()
        ## Pre-train layer-wise
        for i in xrange(dbn.n_layers):
            # go through pretraining epochs
            for epoch in xrange(pretraining_epochs):
                # go through the training set
                c = []
                for batch_index in xrange(n_train_batches):
                    c.append(pretraining_fns[i](index=batch_index,
                                                lr=pretrain_lr))
                print 'Pre-training layer %i, epoch %d, cost ' % (i, epoch),
                print numpy.mean(c)

        end_time = time.clock()
        # end-snippet-2
        print >> sys.stderr, ('The pretraining code for file ' +
                              os.path.split(__file__)[1] +
                              ' ran for %.2fm' % ((end_time - start_time) / 60.))
        cPickle.dump(dbn, open('initial-model.pickle', 'wb'), protocol=cPickle.HIGHEST_PROTOCOL)
    else:
        dbn = cPickle.load(open('initial-model.pickle', 'rb'))
        # dbn = DBN(numpy_rng=numpy_rng, n_ins=raw_x.shape[1],
        #           hidden_layers_sizes=[10, 2])
    ########################
    # FINETUNING THE MODEL #
    ########################

    # get the training, validation and testing function for the model
    print '... getting the finetuning functions'
    train_fn, test_model = dbn.build_finetune_functions(
        train_set_x=train_set_x,
        batch_size=batch_size,
        learning_rate=finetune_lr
    )

    print '... finetuning the model'
    # early-stopping parameters
    patience = 4 * n_train_batches  # look as this many examples regardless
    patience_increase = 4.    # wait this much longer when a new best is
                              # found
    improvement_threshold = 0.995  # a relative improvement of this much is
                                   # considered significant
    validation_frequency = min(n_train_batches, patience / 2)
                                  # go through this many
                                  # minibatches before checking the network
                                  # on the validation set; in this case we
                                  # check every epoch

    best_validation_loss = numpy.inf
    test_score = 0.
    start_time = time.clock()

    done_looping = False
    epoch = 0

    while (epoch < training_epochs) and (not done_looping):
        epoch = epoch + 1
        for minibatch_index in xrange(n_train_batches):

            minibatch_avg_cost = train_fn(minibatch_index)
            iter = (epoch - 1) * n_train_batches + minibatch_index

            if (iter + 1) % validation_frequency == 0:

                validation_losses = test_model()
                this_validation_loss = numpy.mean(validation_losses)
                print(
                    'epoch %i, minibatch %i/%i, validation error %f %%'
                    % (
                        epoch,
                        minibatch_index + 1,
                        n_train_batches,
                        this_validation_loss * 100.
                    )
                )

                # if we got the best validation score until now
                if this_validation_loss < best_validation_loss:

                    #improve patience if loss improvement is good enough
                    if (
                        this_validation_loss < best_validation_loss *
                        improvement_threshold
                    ):
                        patience = max(patience, iter * patience_increase)

                    # save best validation score and iteration number
                    best_validation_loss = this_validation_loss
                    best_iter = iter

            if patience <= iter:
                done_looping = True
                break

    end_time = time.clock()
    print(
        (
            'Optimization complete with best validation score of %f, '
            'obtained at iteration %i, '
        ) % (best_validation_loss, best_iter + 1)
    )
    print >> sys.stderr, ('The fine tuning code for file ' +
                          os.path.split(__file__)[1] +
                          ' ran for %.2fm' % ((end_time - start_time)
                                              / 60.))

    cPickle.dump(dbn, open('total-model.pickle', 'wb'), protocol=cPickle.HIGHEST_PROTOCOL)

def generate():
    dbn = cPickle.load(open('total-model.pickle', 'rb'))
    # raw_x = cPickle.load(open('bach_data.pickle', 'rb'))
    # train_set_x = theano.shared(raw_x)
    # top_level = dbn.label(train_set_x, 0)
    # numpy.savetxt('out.csv', top_level, delimiter=',')

    from midi.utils import midiwrite
    top_level = numpy.random.randint(2, size=[10, 64]).astype(dtype=numpy.float64)
    top_level = theano.shared(top_level)
    output = dbn.generate(top_level)
    output = output.reshape([10, 88*64])
    firstIm = output[0, :].reshape([88, 64])
    outIm = Image.fromarray((firstIm*255).astype('uint8'))
    outIm.save('test.png')
    threshold = 0.1
    firstIm[firstIm > threshold] = 1
    firstIm[firstIm <= threshold] = 0
    midiwrite('test.midi', firstIm.T, r=(12, 109), dt=64)



if __name__ == '__main__':
    train_dbn()