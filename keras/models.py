from __future__ import absolute_import
from __future__ import print_function
import theano
import theano.tensor as T
import numpy as np

from . import optimizers
from . import objectives
import time, copy
from .utils.generic_utils import Progbar
from six.moves import range

import os

def standardize_y(y):
    if not hasattr(y, 'shape'):
        y = np.asarray(y)
    if len(y.shape) == 1:
        y = np.reshape(y, (len(y), 1))
    return y

def make_batches(size, batch_size):
    nb_batch = int(np.ceil(size/float(batch_size)))
    return [(i*batch_size, min(size, (i+1)*batch_size)) for i in range(0, nb_batch)]

class Sequential(object):
    def __init__(self):
        self.layers = []
        self.params = []

    def add(self, layer):
        self.layers.append(layer)
        if len(self.layers) > 1:
            self.layers[-1].connect(self.layers[-2])
        self.params += [p for p in layer.params]

    def compile(self, optimizer, loss, class_mode="categorical"):
        self.optimizer = optimizers.get(optimizer)
        self.loss = objectives.get(loss)

        self.X = self.layers[0].input # input of model 
        # (first layer must have an "input" attribute!)
        self.y_train = self.layers[-1].output(train=True)
        self.y_test = self.layers[-1].output(train=False)

        # output of model
        self.y = T.matrix() # TODO: support for custom output shapes

        train_loss = self.loss(self.y, self.y_train)
        test_score = self.loss(self.y, self.y_test)

        if class_mode == "categorical":
            train_accuracy = T.mean(T.eq(T.argmax(self.y, axis=-1), T.argmax(self.y_train, axis=-1)))
            test_accuracy = T.mean(T.eq(T.argmax(self.y, axis=-1), T.argmax(self.y_test, axis=-1)))

        elif class_mode == "binary":
            train_accuracy = T.mean(T.eq(self.y, T.round(self.y_train)))
            test_accuracy = T.mean(T.eq(self.y, T.round(self.y_test)))
        else:
            raise Exception("Invalid class mode:" + str(class_mode))
        self.class_mode = class_mode

        updates = self.optimizer.get_updates(self.params, train_loss)

        self._train = theano.function([self.X, self.y], train_loss, 
            updates=updates, allow_input_downcast=True)
        self._train_with_acc = theano.function([self.X, self.y], [train_loss, train_accuracy], 
            updates=updates, allow_input_downcast=True)
        self._predict = theano.function([self.X], self.y_test, 
            allow_input_downcast=True)
        self._test = theano.function([self.X, self.y], test_score, 
            allow_input_downcast=True)
        self._test_with_acc = theano.function([self.X, self.y], [test_score, test_accuracy], 
            allow_input_downcast=True)

    def train(self, X, y, accuracy=False):
        y = standardize_y(y)
        if accuracy:
            return self._train_with_acc(X, y)
        else:
            return self._train(X, y)
        

    def test(self, X, y, accuracy=False):
        y = standardize_y(y)
        if accuracy:
            return self._test_with_acc(X, y)
        else:
            return self._test(X, y)


    def fit(self, X, y, batch_size=128, nb_epoch=100, verbose=1,
            validation_split=0., validation_data=None, shuffle=True, show_accuracy=False,
            snapshot_config=None):
        y = standardize_y(y)

        do_validation = False
        if validation_data:
            try:
                X_val, y_val = validation_data
            except:
                raise Exception("Invalid format for validation data; provide a tuple (X_val, y_val).")
            do_validation = True
            y_val = standardize_y(y_val)
            if verbose:
                print("Train on %d samples, validate on %d samples" % (len(y), len(y_val)))
        else:
            if 0 < validation_split < 1:
                # If a validation split size is given (e.g. validation_split=0.2)
                # then split X into smaller X and X_val,
                # and split y into smaller y and y_val.
                do_validation = True
                split_at = int(len(X) * (1 - validation_split))
                (X, X_val) = (X[0:split_at], X[split_at:])
                (y, y_val) = (y[0:split_at], y[split_at:])
                if verbose:
                    print("Train on %d samples, validate on %d samples" % (len(y), len(y_val)))
        
        index_array = np.arange(len(X))

        # Snapshots?
        if snapshot_config is not None:
            do_snapshots = True
            snapshot_folder = snapshot_config['folder']        # path to the snapshot folder, will be created if does not exist
            #snapshot_frequency = snapshot_config['frequency'] # 'epoch', or # of iterations
            #snapshot_optimizer = snapshot_config['optimizer'] # if True, we will pickle the optimizer with each snapshot

            if not os.path.exists(snapshot_folder):
                os.mkdir(snapshot_folder)

        for epoch in range(nb_epoch):
            if verbose:
                print('Epoch', epoch)
                progbar = Progbar(target=len(X), verbose=verbose)
            if shuffle:
                np.random.shuffle(index_array)

            batches = make_batches(len(X), batch_size)
            for batch_index, (batch_start, batch_end) in enumerate(batches):
                if shuffle:
                    batch_ids = index_array[batch_start:batch_end]
                else:
                    batch_ids = slice(batch_start, batch_end)
                X_batch = X[batch_ids]
                y_batch = y[batch_ids]

                if show_accuracy:
                    loss, acc = self._train_with_acc(X_batch, y_batch)
                    log_values = [('loss', loss), ('acc.', acc)]
                else:
                    loss = self._train(X_batch, y_batch)
                    log_values = [('loss', loss)]

                # validation
                if do_validation and (batch_index == len(batches) - 1):
                    if show_accuracy:
                        val_loss, val_acc = self.test(X_val, y_val, accuracy=True)
                        log_values += [('val. loss', val_loss), ('val. acc.', val_acc)]
                    else:
                        val_loss = self.test(X_val, y_val)
                        log_values += [('val. loss', val_loss)]
                
                # logging
                if verbose:
                    progbar.update(batch_end, log_values)
                
            if do_snapshots:
                self.save_weights(os.path.join(snapshot_folder, 'snapshot_{}.hdf5'.format(epoch)))

            
    def predict_proba(self, X, batch_size=128, verbose=1):
        batches = make_batches(len(X), batch_size)
        if verbose==1:
            progbar = Progbar(target=len(X))
        for batch_index, (batch_start, batch_end) in enumerate(batches):
            X_batch = X[batch_start:batch_end]
            batch_preds = self._predict(X_batch)

            if batch_index == 0:
                shape = (len(X),) + batch_preds.shape[1:]
                preds = np.zeros(shape)
            preds[batch_start:batch_end] = batch_preds

            if verbose==1:
                progbar.update(batch_end)

        return preds


    def predict_classes(self, X, batch_size=128, verbose=1):
        proba = self.predict_proba(X, batch_size=batch_size, verbose=verbose)
        if self.class_mode == "categorical":
            return proba.argmax(axis=-1)
        else:
            return (proba>0.5).astype('int32')


    def evaluate(self, X, y, batch_size=128, show_accuracy=False, verbose=1):
        y = standardize_y(y)

        if show_accuracy:
            tot_acc = 0.
        tot_score = 0.

        batches = make_batches(len(X), batch_size)
        if verbose:
            progbar = Progbar(target=len(X), verbose=verbose)
        for batch_index, (batch_start, batch_end) in enumerate(batches):
            X_batch = X[batch_start:batch_end]
            y_batch = y[batch_start:batch_end]

            if show_accuracy:
                loss, acc = self._test_with_acc(X_batch, y_batch)
                tot_acc += acc
                log_values = [('loss', loss), ('acc.', acc)]
            else:
                loss = self._test(X_batch, y_batch)
                log_values = [('loss', loss)]
            tot_score += loss

            # logging
            if verbose:
                progbar.update(batch_end, log_values)

        if show_accuracy:
            return tot_score/len(batches), tot_acc/len(batches)
        else:
            return tot_score/len(batches)

    def describe(self, verbose=1):
        layers = []
        for i, l in enumerate(self.layers):
            config = l.get_config()
            layers.append(config)
            if verbose:
                print('Layer %d: %s' % (i, config.get('name', '?')))
                for k, v in config.items():
                    if k != 'name':
                        print('... ' + k + ' = ' + str(v))
        return layers

    def save_weights(self, filepath):
        # Save weights from all layers to HDF5
        import h5py
        # FIXME: fail if file exists, or add option to overwrite!
        f = h5py.File(filepath, 'w')
        f.attrs['nb_layers'] = len(self.layers)
        for k, l in enumerate(self.layers):
            g = f.create_group('layer_{}'.format(k))
            weights = l.get_weights()
            g.attrs['nb_params'] = len(weights)
            for n, param in enumerate(weights):
                param_name = 'param_{}'.format(n)
                param_dset = g.create_dataset(param_name, param.shape, dtype='float64')
                param_dset[:] = param
            for k, v in l.get_config().items():
                if v is not None:
                    g.attrs[k] = v
        f.flush()
        f.close()

    def load_weights(self, filepath):
        # Loads weights from HDF5 file
        import h5py
        f = h5py.File(filepath)
        for k in range(f.attrs['nb_layers']):
            g = f['layer_{}'.format(k)]
            weights = [g['param_{}'.format(p)] for p in range(g.attrs['nb_params'])]
            self.layers[k].set_weights(weights)
        f.close()


