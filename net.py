#==========================================================================#
# Copyright (C) 2016 Hosang Yoon (hosangy@gmail.com) - All Rights Reserved #
# Unauthorized copying of this file, via any medium is strictly prohibited #
#                       Proprietary and confidential                       #
#==========================================================================#

"""
Class for setting up graphs and compiling updater functions
See README.md for a summary of this class
"""

import cPickle as pk
from collections import OrderedDict
import os

from layers import FCLayer, LSTMLayer, GRULayer
from utils import MSE_loss, clip_norm, get_random_string
from optimizers import sgd_update, momentum_update, nesterov_update, \
                       vanilla_force, adadelta_force, rmsprop_force, adam_force

import numpy as np
import theano as th
import theano.tensor as tt

class Net():
    def __init__(self, options = None, save_to = None, load_from = None):
        """
        Mode is determined by whether save_to is None or not
        Training:
            <options>   OrderedDict { 'option_name' : option_val     }
            <save_to>   str         'path'
            [load_from] str         'path' (set for re-annealing)
        Inference:
            [options]   OrderedDict { 'batch_size'  : new_batch_size }
            (save_to)   NoneType    (leave as none)
            <load_from> str         'path'
            For inference, only options['batch_size'] is applicable 
        """
        self._configure(options, save_to, load_from)    
        self._init_params(load_from)
        self._init_shared_variables()
        if self._is_training:
            self._setup_training_graph()
        else:
            self._setup_inference_graph()
    
    def _configure(self, options, save_to, load_from):
        if save_to is not None:
            self._is_training = True

            assert options is not None
            self._options = options

            self._save_to = save_to
            self._pfx = ''
            
            if load_from is not None:
                with open(load_from + '/options.pkl', 'rb') as f:
                    loaded_options = pk.load(f)
                    if self._options != loaded_options:
                        print '[Error] Mismatching options in loaded model'
                        assert False
            
            with open(save_to + '/options.pkl', 'wb') as f:
                pk.dump(self._options, f)
        else:
            self._is_training = False

            self._pfx = get_random_string() + '_' # avoid name clash in ensemble
            
            assert load_from is not None
            with open(load_from + '/options.pkl', 'rb') as f:
                self._options = pk.load(f)
            
            assert 'batch_size' in options
            self._options['batch_size'] = options['batch_size']
 
    def _init_params(self, load_from):
        """
        Instantiate layers and store their learnable parameters
        Load parameter values from file if applicable
        """
        self._params = OrderedDict()

        if not self._options['learn_id_embedding']:
            add = 0
        else:
            self._id_embedder = FCLayer(self._pfx + 'FC_id_embedder')
            self._id_embedder.add_param(params  = self._params,
                                        n_in    = self._options['id_count'],
                                        n_out   = self._options['id_embedding_dim'],
                                        options = self._options,
                                        act     = 'lambda x: x')
            add = self._options['id_embedding_dim']

        self._layers = []
        D = self._options['net_depth']
        assert D > 0
        for i in range(D):
            self._layers.append(eval(self._options['unit_type'] + 'Layer') \
                                     (self._pfx + self._options['unit_type'] + '_' + str(i)))
            self._layers[i].add_param(params  = self._params,
                                      n_in    = add + \
                                                (self._options['net_width'] if i > 0 else \
                                                 self._options['input_dim']),
                                      n_out   = self._options['net_width'],
                                      options = self._options)
        self._layers.append(FCLayer(self._pfx + 'FC_output'))
        self._layers[D].add_param    (params  = self._params,
                                      n_in    = add + self._options['net_width'],
                                      n_out   = self._options['target_dim'],
                                      options = self._options,
                                      act     = 'lambda x: x')
        
        if load_from is not None:
            len_pfx = len(self._pfx)
            params = np.load(load_from + '/params.npz') # NpzFile object
            for k in self._params.iterkeys():
                self._params[k] = params[k[len_pfx:]] # saved params don't have prefix

    def _init_shared_variables(self):
        """
        Declare and store shared variables for parameters, gradients, and prev_states
        For options['learn_init_states'], init states are not directly learnable 
        (gradients are taken wrt v_prev_states, then applied to init states),
        and hence stored separately from the rest
        """
        self._v_params = OrderedDict()
        self._v_params_wo_init  = OrderedDict()
        for k, v in self._params.iteritems():
            self._v_params[k] = th.shared(v, name = k)
            if k[-4:] != 'init':
                self._v_params_wo_init[k] = self._v_params[k]

        self._v_prev_states = OrderedDict()
        for layer in self._layers:
            layer.add_v_prev_state(self._v_prev_states)

        if self._is_training:
            self._v_grads = [th.shared(v.get_value() * 0., name = k + '_grad') \
                             for k, v in self._v_params_wo_init.iteritems()]

            if self._options['learn_init_states']:
                self._v_params_for_init  = OrderedDict()
                for k, v in self._v_params.iteritems():
                    if k[-4:] == 'init':
                        self._v_params_for_init[k] = v
                
                self._v_grads_for_init = [th.shared(v.get_value() * 0., name = k + '_grad') \
                                          for k, v in self._v_prev_states.iteritems()]

    def _setup_forward_graph(self, s_input_tbi, s_time_t, s_id_idx_b,
                             s_last_tap, v_params, v_prev_states):
        """
        Specify layer connections
        Layers return their final internal states (to be used for the next time sequence) as
            prev_state_update = (v_prev_state, s_new_prev_state)
        which are collected as a list and returned along with the last layer's output
        """
        if not self._options['learn_id_embedding']:
            cat = lambda s_below_tbj: s_below_tbj
        else:
            s_id_emb_bi, _ = self._id_embedder. \
                setup_graph(s_below_tbj   = tt.extra_ops.to_one_hot \
                                                (s_id_idx_b, self._options['id_count']),
                            s_time_t      = None,
                            s_last_tap    = s_last_tap,
                            v_params      = v_params,
                            v_prev_states = v_prev_states)
            s_id_emb_tbi = tt.tile(s_id_emb_bi, (s_input_tbi.shape[0], 1, 1))
            cat = lambda s_below_tbj: tt.concatenate([s_below_tbj, s_id_emb_tbi], axis = 2)

        D = self._options['net_depth']
        s_outputs = [None] * (D + 1)  # (RNN) * D + FC
        s_outputs.append(s_input_tbi) # put input at index -1
        prev_state_updates = []

        # vertical stack: input -> layer[0] -> ... -> layer[D - 1] -> output
        for i in range(D + 1):
            s_outputs[i], update = self._layers[i]. \
                setup_graph(s_below_tbj   = cat(s_outputs[i - 1]),
                            s_time_t      = s_time_t if self._options['learn_clock_params'] else None,
                            s_last_tap    = s_last_tap,
                            v_params      = v_params,
                            v_prev_states = v_prev_states)
            if update is not None:
                prev_state_updates.append(update)
        
        return s_outputs[D], prev_state_updates

    def _setup_inference_graph(self):
        """
        Connect graphs together for inference and store input/output ports and updates
            Input port : _port_i_input, _port_i_time, _port_i_id_idx
            Output port: _port_o_output
            Updates    : _prev_state_updates
        """
        self._port_i_input_tbi = tt.tensor3(name  = 'port_i_input' , dtype = 'float32')
        self._port_i_time_t    = tt.vector (name  = 'port_i_time'  , dtype = 'float32')
        self._port_i_id_idx_b  = tt.vector (name  = 'port_i_id_idx', dtype = 'int32'  )
        
        s_last_tap = self._port_i_input_tbi.shape[0] - 1 # always fixed at last step

        self._port_o_output, self._prev_state_updates = \
                           self._setup_forward_graph(s_input_tbi   = self._port_i_input_tbi,
                                                     s_time_t      = self._port_i_time_t,
                                                     s_id_idx_b    = self._port_i_id_idx_b,
                                                     s_last_tap    = s_last_tap,
                                                     v_params      = self._v_params,
                                                     v_prev_states = self._v_prev_states)

    def _setup_loss_graph(self, s_output_tbi, s_target_tbi, s_loss_tap):
        """
        Connect a loss function to the graph
        """
        return MSE_loss(s_output_tbi[s_loss_tap :], s_target_tbi[s_loss_tap :])

    def _setup_grad_updates_graph(self, s_loss, v_wrt, v_grads):
        """
        Connect loss to new values of gradients
            grad_update = (v_grad, s_new_grad)
        Takes inputs as lists instead of OrderedDict
        """
        assert len(v_wrt) == len(v_grads)
        s_new_grads = tt.grad(s_loss, wrt = v_wrt)
        if 'grad_norm_clip' in self._options:
            s_new_grads = [clip_norm(s_grad, self._options['grad_norm_clip']) \
                           for s_grad in s_new_grads]
        return zip(v_grads, s_new_grads)

    def _setup_param_updates_graph(self, s_lr, v_params, v_grads):
        """
        Connect learning rate, parameters, gradients, and internal states of optimizer to
        new values of parameters, and inform how optimizer should be initiated/updated
            optim_state_init = (v_optim_state, s_init_optim_state)
            param_update     = (v_param, s_new_param) or (v_optim_state, s_new_optim_state)
        Takes inputs as lists instead of OrderedDict
        Assumes that v_grads has been updated prior to applying param_updates returned here
        """
        assert len(v_params) == len(v_grads)
        optim_f_inits, optim_f_updates, s_forces = \
            eval(self._options['force_type'] + '_force')(options = self._options,
                                                         s_lr    = s_lr,
                                                         v_grads = v_grads)
        assert len(v_params) == len(s_forces)
        optim_u_inits, optim_u_param_updates = \
            eval(self._options['update_type'] + '_update')(options  = self._options,
                                                           v_params = v_params,
                                                           s_forces = s_forces)
        return optim_f_inits + optim_u_inits, optim_f_updates + optim_u_param_updates

    def _setup_training_graph(self):
        """
        Connect graphs together for training and store input/output ports and updates
        Input ports: _port_i_input, _port_i_target, _port_i_time, _port_i_id_idx_b,
                     _port_i_last_tap, _port_i_loss_tap, _port_i_lr, 
        Output port: _port_o_loss
        Updates    : _prev_state_updates, _grad_updates, _optim_state_inits, _param_updates
        """
        self._port_i_input_tbi  = tt.tensor3(name = 'port_i_input'   , dtype = 'float32')
        self._port_i_target_tbi = tt.tensor3(name = 'port_i_target'  , dtype = 'float32')
        self._port_i_time_t     = tt.vector (name = 'port_i_time'    , dtype = 'float32')
        self._port_i_id_idx_b   = tt.vector (name = 'port_i_id_idx'  , dtype = 'int32'  )
        self._port_i_last_tap   = tt.scalar (name = 'port_i_last_tap', dtype = 'int32'  )
        self._port_i_loss_tap   = tt.scalar (name = 'port_i_loss_tap', dtype = 'int32'  )
        self._port_i_lr         = tt.scalar (name = 'port_i_lr'      , dtype = 'float32')

        s_output_tbi, self._prev_state_updates = \
                           self._setup_forward_graph(s_input_tbi   = self._port_i_input_tbi,
                                                     s_time_t      = self._port_i_time_t,
                                                     s_id_idx_b    = self._port_i_id_idx_b,
                                                     s_last_tap    = self._port_i_last_tap,
                                                     v_params      = self._v_params,
                                                     v_prev_states = self._v_prev_states)

        self._port_o_loss = self._setup_loss_graph(s_output_tbi = s_output_tbi,
                                                   s_target_tbi = self._port_i_target_tbi,
                                                   s_loss_tap   = self._port_i_loss_tap)

        self._grad_updates  = self._setup_grad_updates_graph (s_loss   = self._port_o_loss,
                                                              v_wrt    = self._v_params_wo_init.values(),
                                                              v_grads  = self._v_grads)
        self._optim_state_inits, \
        self._param_updates = self._setup_param_updates_graph(s_lr     = self._port_i_lr,
                                                              v_params = self._v_params_wo_init.values(),
                                                              v_grads  = self._v_grads)
        
        if self._options['learn_init_states']:
            self._grad_updates_for_init = self._setup_grad_updates_graph\
                                                             (s_loss   = self._port_o_loss,
                                                              v_wrt    = self._v_prev_states.values(),
                                                              v_grads  = self._v_grads_for_init)
            
            s_grads_for_init_flattened = [tt.mean(v, axis = 0) for v in self._v_grads_for_init]
            optim_state_inits_for_init, \
            self._param_updates_for_init = self._setup_param_updates_graph\
                                                             (s_lr     = self._port_i_lr,
                                                              v_params = self._v_params_for_init.values(),
                                                              v_grads  = s_grads_for_init_flattened)
            self._optim_state_inits.extend(optim_state_inits_for_init)
        
    def compile_f_initialize_states(self):
        """
        Compile a callable object of signature
            f() -> None
        As a side effect, calling it updates
            _v_prev_states['$_prev'] <- 0-tensors           (if not options['learn_init_states'])
                                        _v_params['$_init'] (if     options['learn_init_states'])
        For both training & inference;
            needs to be called before launching a sequence at t = 0
        """
        if not self._options['learn_init_states']:
            updates = [(v, tt.zeros_like(v)) for v in self._v_prev_states.itervalues()]
        else:
            to_init = lambda k: k[:-4] + 'init'
            updates = [(v, tt.tile(self._v_params[to_init(k)], (self._options['batch_size'], 1))) \
                       for k, v in self._v_prev_states.iteritems()]
        return th.function(inputs  = [],
                           outputs = [],
                           updates = updates)

    def compile_f_fwd_propagate(self):
        """
        Compile a callable object of signature
            f(input_tbi, target_tbi, time_t,
              id_idx_b, last_tap, loss_tap) -> loss         (for training)
            f(input_tbi, time_t, id_idx_b)  -> output_tbi   (for inference)
        As a side effect, calling it updates
            _v_prev_states <- _prev_state_updates
        Note that output is a list of np.ndarray (i.e., 0-th element is np.ndarray)
        """
        if self._is_training:
            return th.function(inputs  = [self._port_i_input_tbi, self._port_i_target_tbi,
                                          self._port_i_time_t   , self._port_i_id_idx_b,
                                          self._port_i_last_tap , self._port_i_loss_tap],
                               outputs = [self._port_o_loss],
                               updates = self._prev_state_updates,
                       on_unused_input = 'raise' if self._options['learn_clock_params'] \
                                                 or self._options['learn_id_embedding'] \
                                                 else 'ignore')
        else:
            return th.function(inputs  = [self._port_i_input_tbi, self._port_i_time_t,
                                          self._port_i_id_idx_b],
                               outputs = [self._port_o_output],
                               updates = self._prev_state_updates,
                       on_unused_input = 'raise' if self._options['learn_clock_params'] \
                                                 or self._options['learn_id_embedding'] \
                                                 else 'ignore')

    def compile_f_fwd_bwd_propagate(self):
        """
        Compile a callable object of signature
            f(input_tbi, target_tbi, time_t, id_idx_b) -> loss
        As a side effect, calling it updates
            _v_grads       <- _grad_updates
            _v_prev_states <- _prev_state_updates
        Note that output is a list of np.ndarray (even if 1 output & scalar)
        To get loss but not update params (for validation), call f_fwd_propagate instead
        """
        assert self._is_training
        return th.function(inputs  = [self._port_i_input_tbi, self._port_i_target_tbi,
                                      self._port_i_time_t   , self._port_i_id_idx_b,
                                      self._port_i_last_tap , self._port_i_loss_tap],
                           outputs = [self._port_o_loss],
                           updates = self._grad_updates + self._prev_state_updates,
                           on_unused_input = 'raise' if self._options['learn_clock_params'] \
                                                     or self._options['learn_id_embedding'] \
                                                     else 'ignore')
    
    def compile_f_update_v_params(self):
        """
        Compile a callable object of signature
            f(lr) -> None
        As a side effect, calling it updates
            _v_params <- _param_updates
        Because it uses _v_grads, f_fwd_bwd_propagate must be called before f_update_v_params
        To get loss but not update params (for validation), don't call f_update_v_params
        """
        assert self._is_training
        return th.function(inputs  = [self._port_i_lr],
                           outputs = [],
                           updates = self._param_updates)
    
    def compile_f_fwd_bwd_for_init(self):
        """
        NOTE: For the first time step during options['learn_init_states'],
              call f_fwd_bwd_for_init *instead of* f_fwd_bwd_propagate
        """
        assert self._is_training and self._options['learn_init_states']
        return th.function(inputs  = [self._port_i_input_tbi, self._port_i_target_tbi,
                                      self._port_i_time_t   , self._port_i_id_idx_b,
                                      self._port_i_last_tap , self._port_i_loss_tap],
                           outputs = [self._port_o_loss],
                           updates = self._grad_updates + self._prev_state_updates + \
                                     self._grad_updates_for_init,
                           on_unused_input = 'raise' if self._options['learn_clock_params'] \
                                                     or self._options['learn_id_embedding'] \
                                                     else 'ignore')
    
    def compile_f_update_init_states(self):
        """
        NOTE: For the first time step during options['learn_init_states'],
              call f_update_init_states *in addition to* f_update_v_params
        """
        assert self._is_training and self._options['learn_init_states']
        return th.function(inputs  = [self._port_i_lr],
                           outputs = [],
                           updates = self._param_updates_for_init)

    def compile_f_initialize_optimizer(self):
        """
        Compile a callable object of signature
            f() -> None
        As a side effect, calling it updates
            v_optim_states <- s_init_optim_states (these are stored indirectly in this class)
        Call f_initialize_optimizer when learning rate has changed
        """
        assert self._is_training
        return th.function(inputs  = [],
                           outputs = [],
                           updates = self._optim_state_inits)

    def save_v_params_to_workspace(self, name = 'params'):
        """
        Transfer parameters from GPU to file
        Extension '.npz' appended inside
        For use during annealing
        """
        assert self._is_training
        for k, v_param in self._v_params.iteritems():
            self._params[k] = v_param.get_value() # pull parameters from GPU

        # There is also savez_compressed, but parameter data
        # doesn't offer much opportunities for compression
        if name is None:
            name = 'params'
        np.savez(self._save_to + '/' + name + '.npz', **self._params)

    def load_v_params_from_workspace(self, name = 'params'):
        """
        Transfer parameters from file to GPU
        Extension '.npz' appended inside
        For use during annealing
        """
        assert self._is_training
        if name is None:
            name = 'params'
        params = np.load(self._save_to + '/' + name + '.npz') # NpzFile object
        for k, v_param in self._v_params.iteritems():
            v_param.set_value(params[k]) # push parameters to GPU
    
    def remove_params_file_from_workspace(self, name = 'params'):
        """
        Remove temporary file from the workspace
        Extension '.npz' appended inside
        For use during annealing
        """
        assert self._is_training
        if name is None:
            name = 'params'
        os.remove(self._save_to + '/' + name + '.npz')
    
    def dimensions(self):
        """
        Intended for use during inference
        """
        return self._options['input_dim'], self._options['target_dim']
    
    def save_param(self, param_name, filename):
        """
        Cannot be used during training as params is not kept up to date
        """
        assert not self._is_training and self._pfx + param_name in self._params
        np.savez(filename, **{ param_name : self._params[self._pfx + param_name] })
