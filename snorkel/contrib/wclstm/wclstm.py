import os
import warnings

import torch
import torch.nn as nn
import torch.utils.data as data_utils
from torch.autograd import Variable

from snorkel.learning.disc_learning import TFNoiseAwareModel
from snorkel.learning.utils import reshape_marginals, LabelBalancer
from utils import *
from layers import *
from six.moves.cPickle import dump, load
from time import time


class WCLSTM(TFNoiseAwareModel):
    name = 'WCLSTM'
    representation = True
    char_marker = ['<', '>']
    gpu = ['gpu', 'GPU']

    # Set unknown
    unknown_symbol = 1

    """Hierarchy Bi-LSTM for relation extraction"""

    def _preprocess_data(self, candidates, extend=False):
        """Convert candidate sentences to lookup sequences

        :param candidates: candidates to process
        :param extend: extend symbol table for tokens (train), or lookup (test)?
        """
        if not hasattr(self, 'word_dict'):
            self.word_dict = SymbolTable()
            # Add paddings for words
            map(self.word_dict.get, ['~~[[1', '1]]~~', '~~[[2', '2]]~~'])

        if not hasattr(self, 'char_dict'):
            self.char_dict = SymbolTable()
            # Add paddings for chars
            map(self.char_dict.get, self.char_marker)

        word_seq_data = []
        char_seq_data = []
        for candidate in candidates:
            # Mark sentence based on cardinality of relation
            if len(candidate) == 2:
                args = [
                    (candidate[0].get_word_start(), candidate[0].get_word_end(), 1),
                    (candidate[1].get_word_start(), candidate[1].get_word_end(), 2)
                ]
            else:
                args = [(candidate[0].get_word_start(), candidate[0].get_word_end(), 1)]

            s = mark_sentence(candidate_to_tokens(candidate), args)
            # Either extend word table or retrieve from it
            f = self.word_dict.get if extend else self.word_dict.lookup
            word_seq_data.append(np.array(map(f, s)))

            # Either extend char table or retrieve from it
            g = self.char_dict.get if extend else self.char_dict.lookup
            char_seq = []
            for w in s:
                word = self.char_marker[0] + w + self.char_marker[1]
                word_char_seq = []
                for i in range(len(word) - self.char_gram + 1):
                    word_char_seq.append(word[i:i + self.char_gram])
                char_seq.append(np.array(map(g, word_char_seq)))
            char_seq_data.append(char_seq)
        return np.array(word_seq_data), np.array(char_seq_data)

    def _check_max_sentence_length(self, ends, max_len=None):
        """Check that extraction arguments are within @self.max_len"""
        mx = max_len or self.max_sentence_length
        for i, end in enumerate(ends):
            if end >= mx:
                w = "Candidate {0} has argument past max length for model:"
                info = "[arg ends at index {0}; max len {1}]".format(end, mx)
                warnings.warn('\t'.join([w.format(i), info]))

    def create_dict(self, splits, word=True, char=True):
        """Create global dict from user input"""
        if word:
            self.word_dict = SymbolTable()
            self.word_dict_all = {}

            # Add paddings for words
            map(self.word_dict.get, ['~~[[1', '1]]~~', '~~[[2', '2]]~~'])

        if char:
            self.char_dict = SymbolTable()
            self.char_dict_all = {}

            # Add paddings for chars
            map(self.char_dict_all.get, self.char_marker)

        # Initialize training vocabulary
        for candidate in splits["train"]:
            words = candidate_to_tokens(candidate)
            if word: map(self.word_dict.get, words)
            if char: map(self.char_dict.get, list(' '.join(words)))

        # Initialize pre-trained vocabulary
        for candset in splits["test"]:
            for candidate in candset:
                words = candidate_to_tokens(candidate)
                if word:
                    self.word_dict_all.update(dict.fromkeys(words))
                if word:
                    self.char_dict_all.update(dict.fromkeys(list(' '.join(words))))

        print "|Train Vocab|    = {}".format(self.word_dict.s)
        print "|Dev/Test Vocab| = {}".format(len(self.word_dict_all))

    def load_dict(self):
        """Load dict from user input embeddings"""
        if not hasattr(self, 'word_dict'):
            self.word_dict = SymbolTable()

        if not hasattr(self, 'char_dict'):
            self.char_dict = SymbolTable()

        # Add paddings
        map(self.word_dict.get, ['~~[[1', '1]]~~', '~~[[2', '2]]~~'])

        # Add paddings for chars
        map(self.char_dict.get, self.char_marker)

        # Word embeddings
        f = open(self.word_emb_path, 'r')
        fmt = "fastText" if self.word_emb_path.split(".")[-1] == "vec" else "txt"

        n, N = 0.0, 0.0

        l = list()
        for i, _ in enumerate(f):
            if fmt == "fastText" and i == 0: continue
            line = _.strip().split(' ')
            assert (len(line) == self.word_emb_dim + 1), "Word embedding dimension doesn't match!"
            word = line[0]
            # Replace placeholder to original word defined by user.
            for key in self.replace.keys():
                word = word.replace(key, self.replace[key])
            if hasattr(self, 'word_dict_all') and word in self.word_dict_all:
                l.append(word)
                n += 1

        map(self.word_dict.get, l)
        if hasattr(self, 'word_dict_all'):
            N = len(self.word_dict_all)
            print "|Dev/Test Vocab|                   = {}".format(N)
            print "|Dev/Test Vocab ^ Pretrained Embs| = {} {:2.2f}%".format(n, n / float(N) * 100)
            print "|Vocab|                            = {}".format(self.word_dict.s)
        f.close()

        # Char embeddings
        f = open(self.char_emb_path, 'r')

        l = list()
        for _ in f:
            line = _.strip().split(' ')
            assert (self.char_emb_dim + 1 == len(line)), "Char embedding dimension doesn't match!"
            char = line[0]
            # Replace placeholder to original word defined by user.
            for key in self.replace.keys():
                char = char.replace(key, self.replace[key])
            if hasattr(self, 'char_dict_all') and char in self.char_dict_all:
                l.append(char)

        map(self.char_dict.get, l)
        f.close()

    def load_embeddings(self):
        """Load pre-trained embeddings from user input"""
        self.load_dict()
        # Random initial word embeddings
        self.word_emb = np.random.uniform(-0.1, 0.1, (self.word_dict.s, self.word_emb_dim)).astype(np.float)

        # Random initial char embeddings
        self.char_emb = np.random.uniform(-0.1, 0.1, (self.char_dict.s, self.char_emb_dim)).astype(np.float)

        # Word embeddings
        f = open(self.word_emb_path, 'r')
        fmt = "fastText" if self.word_emb_path.split(".")[-1] == "vec" else "txt"

        for i, line in enumerate(f):
            if fmt == "fastText" and i == 0:
                continue
            line = line.strip().split(' ')
            assert (len(line) == self.word_emb_dim + 1), "Word embedding dimension doesn't match!"
            for key in self.replace.keys():
                line[0] = line[0].replace(key, self.replace[key])
            if self.word_dict.lookup(line[0]) != self.unknown_symbol:
                self.word_emb[self.word_dict.lookup_strict(line[0])] = np.asarray(
                    [float(_) for _ in line[-self.word_emb_dim:]])
        f.close()

        # Char embeddings
        f = open(self.char_emb_path, 'r')

        for line in f:
            line = line.strip().split(' ')
            assert (len(line) == self.char_emb_dim + 1), "Char embedding dimension doesn't match!"
            for key in self.replace.keys():
                line[0] = line[0].replace(key, self.replace[key])
            if self.char_dict.lookup(line[0]) != self.unknown_symbol:
                self.char_emb[self.char_dict.lookup_strict(line[0])] = np.asarray(
                    [float(_) for _ in line[-self.char_emb_dim:]])
        f.close()

    def train_model(self, w_model, c_model, optimizer, criterion, x_w, x_w_mask, x_c, x_c_mask, y):
        """Train LSTM model"""
        w_model.train()
        c_model.train()
        batch_size, max_sent, max_token = x_c.size()
        w_state_word = w_model.init_hidden(batch_size)
        c_state_word = c_model.init_hidden(batch_size)

        if self.host_device in self.gpu:
            x_w = x_w.cuda()
            x_w_mask = x_w_mask.cuda()
            x_c = x_c.cuda()
            x_c_mask = x_c_mask.cuda()
            y = y.cuda()
            w_state_word = (w_state_word[0].cuda(), w_state_word[1].cuda())
            c_state_word = (c_state_word[0].cuda(), c_state_word[1].cuda())

        optimizer.zero_grad()
        s = None
        for i in xrange(max_sent):
            _s = c_model(x_c[:, i, :], x_c_mask[:, i, :], c_state_word)
            _s = _s.unsqueeze(0)
            s = _s if s is None else torch.cat((s, _s), 0)
        s = s.transpose(0, 1)
        y_pred = w_model(x_w, x_w_mask, s, w_state_word)

        if self.host_device in self.gpu:
            loss = criterion(y_pred.cuda(), y)
        else:
            loss = criterion(y_pred, y)

        loss.backward()
        optimizer.step()
        return loss.data[0]

    def _init_kwargs(self, **kwargs):
        """Parse user input arguments"""
        self.model_kwargs = kwargs

        if kwargs.get('init_pretrained', False):
            self.create_dict(kwargs['init_pretrained'], word=True, char=True)

        # Set use pre-trained embedding or not
        self.load_emb = kwargs.get('load_emb', False)

        # Set word embedding dimension
        self.word_emb_dim = kwargs.get('word_emb_dim', 300)
        # Set char embedding dimension
        self.char_emb_dim = kwargs.get('char_emb_dim', 300)

        # Set word embedding path
        self.word_emb_path = kwargs.get('word_emb_path', None)
        # Set char embedding path
        self.char_emb_path = kwargs.get('char_emb_path', None)

        # Set learning rate
        self.lr = kwargs.get('lr', 1e-3)

        # Set use attention or not
        self.attention = kwargs.get('attention', True)

        # Set learning epoch
        self.n_epochs = kwargs.get('n_epochs', 100)

        # Set dropout
        self.dropout = kwargs.get('dropout', 0.0)

        # Set learning batch size
        self.batch_size = kwargs.get('batch_size', 100)

        # Set rebalance setting
        self.rebalance = kwargs.get('rebalance', False)

        # Set char gram k
        self.char_gram = kwargs.get('char_gram', 3)

        # Set lstm hidden dimension
        self.lstm_hidden_dim = kwargs.get('lstm_hidden_dim', 100)

        # Set bidirectional or not
        self.bidirectional = kwargs.get('bidirectional', True)

        # Set max sentence length
        self.max_sentence_length = kwargs.get('max_sentence_length', 100)

        # Set max word length
        self.max_word_length = kwargs.get('max_word_length', 20)

        # Set host device
        self.host_device = kwargs.get('host_device', 'cpu')

        # Replace placeholders in embedding files
        self.replace = kwargs.get('replace', {})

        # Set patience (number of epochs to wait without model improvement)
        self.patience = kwargs.get('patience', 100)

        print "==============================================="
        print "Number of learning epochs:     ", self.n_epochs
        print "Learning rate:                 ", self.lr
        print "Use attention:                 ", self.attention
        print "LSTM hidden dimension:         ", self.lstm_hidden_dim
        print "dropout:                       ", self.dropout
        print "Batch size:                    ", self.batch_size
        print "Rebalance:                     ", self.rebalance
        print "Checkpoint Patience:           ", self.patience
        print "Char gram:                     ", self.char_gram
        print "Max word length:               ", self.max_word_length
        print "Max sentence length:           ", self.max_sentence_length
        print "Load pre-trained embedding:    ", self.load_emb
        print "Host device:                   ", self.host_device
        print "Word embedding size:           ", self.word_emb_dim
        print "Char embedding size:           ", self.char_emb_dim
        print "Word embedding:                ", self.word_emb_path
        print "Char embedding:                ", self.char_emb_path
        print "==============================================="

        if self.load_emb:
            assert self.word_emb_path is not None
            assert self.char_emb_path is not None

        if "init_pretrained" in kwargs:
            del self.model_kwargs["init_pretrained"]

    def train(self, X_train, Y_train, X_dev=None, Y_dev=None, print_freq=5, dev_ckpt=True,
              dev_ckpt_delay=0.75, save_dir='checkpoints', **kwargs):

        """
        Perform preprocessing of data, construct dataset-specific model, then
        train.
        """

        self._init_kwargs(**kwargs)

        verbose = print_freq > 0

        # Set random seed
        torch.manual_seed(self.seed)
        if self.host_device in self.gpu:
            torch.cuda.manual_seed(self.seed)

        np.random.seed(seed=int(self.seed))

        # Set random seed for all numpy operations
        self.rand_state.seed(self.seed)

        cardinality = Y_train.shape[1] if len(Y_train.shape) > 1 else 2
        if cardinality != self.cardinality:
            raise ValueError("Training marginals cardinality ({0}) does not"
                             "match model cardinality ({1}).".format(Y_train.shape[1],
                                                                     self.cardinality))
        # Make sure marginals are in correct default format
        Y_train = reshape_marginals(Y_train)
        # Make sure marginals are in [0,1] (v.s e.g. [-1, 1])
        if self.cardinality > 2 and not np.all(Y_train.sum(axis=1) - 1 < 1e-10):
            raise ValueError("Y_train must be row-stochastic (rows sum to 1).")
        if not np.all(Y_train >= 0):
            raise ValueError("Y_train must have values in [0,1].")

        if self.cardinality == 2:
            # This removes unlabeled examples and optionally rebalances
            train_idxs = LabelBalancer(Y_train).get_train_idxs(self.rebalance,
                                                               rand_state=self.rand_state)
        else:
            # In categorical setting, just remove unlabeled
            diffs = Y_train.max(axis=1) - Y_train.min(axis=1)
            train_idxs = np.where(diffs > 1e-6)[0]
        X_train = [X_train[j] for j in train_idxs] if self.representation \
            else X_train[train_idxs, :]
        Y_train = Y_train[train_idxs]

        if verbose:
            st = time()
            print "[%s] n_train= %s" % (self.name, len(X_train))

        X_w_train, X_c_train = self._preprocess_data(X_train, extend=True)

        if self.load_emb:
            # load embeddings from file
            self.load_embeddings()

            print "Done loading pre-trained embeddings..."

        Y_train = torch.from_numpy(Y_train).float()

        X = torch.from_numpy(np.arange(len(X_w_train)))
        data_set = data_utils.TensorDataset(X, Y_train)
        data_loader = data_utils.DataLoader(data_set, batch_size=self.batch_size, shuffle=False)

        n_classes = 1 if self.cardinality == 2 else None

        self.char_model = CharRNN(batch_size=self.batch_size, num_tokens=self.char_dict.s,
                                  embed_size=self.char_emb_dim,
                                  lstm_hidden=self.lstm_hidden_dim,
                                  attention=self.attention,
                                  dropout=self.dropout,
                                  bidirectional=self.bidirectional,
                                  use_cuda=self.host_device in self.gpu)
        if self.load_emb:
            # Set pre-trained embedding weights
            self.char_model.lookup.weight.data.copy_(torch.from_numpy(self.char_emb))

        b = 2 if self.bidirectional else 1
        self.word_model = WordRNN(n_classes=n_classes, batch_size=self.batch_size,
                                  num_tokens=self.word_dict.s,
                                  embed_size=self.word_emb_dim,
                                  input_size=self.word_emb_dim + b * self.lstm_hidden_dim,
                                  lstm_hidden=self.lstm_hidden_dim,
                                  attention=self.attention,
                                  dropout=self.dropout,
                                  bidirectional=self.bidirectional,
                                  use_cuda=self.host_device in self.gpu)

        if self.load_emb:
            # Set pre-trained embedding weights
            self.word_model.lookup.weight.data.copy_(torch.from_numpy(self.word_emb))

        if self.host_device in self.gpu:
            self.char_model.cuda()
            self.word_model.cuda()

        n_examples = len(X_w_train)

        optimizer = torch.optim.Adam(list(self.char_model.parameters()) + list(self.word_model.parameters()),
                                     lr=self.lr)
        loss = nn.MultiLabelSoftMarginLoss(size_average=False)

        dev_score_opt = 0.0
        last_epoch_opt = None

        for idx in range(self.n_epochs):
            cost = 0.
            for x, y in data_loader:
                x_w, x_w_mask, x_c, x_c_mask = pad_batch(X_w_train[x.numpy()], X_c_train[x.numpy()],
                                                         self.max_sentence_length, self.max_word_length)
                y = Variable(y.float(), requires_grad=False)
                cost += self.train_model(self.word_model, self.char_model, optimizer, loss, x_w, x_w_mask, x_c, x_c_mask, y)
            if verbose and ((idx + 1) % print_freq == 0 or idx + 1 == self.n_epochs):
                msg = "[%s] Epoch %s, Training error: %s" % (self.name, idx + 1, cost / n_examples)
                if X_dev is not None:
                    scores = self.score(X_dev, Y_dev, batch_size=self.batch_size)
                    score = scores if self.cardinality > 2 else scores[-1]
                    score_label = "Acc." if self.cardinality > 2 else "F1"
                    msg += '\tDev {0}={1:.2f}'.format(score_label, 100. * score)
                print msg

                if X_dev is not None and dev_ckpt and idx > dev_ckpt_delay * self.n_epochs and score > dev_score_opt:
                    dev_score_opt = score
                    self.save(save_dir=save_dir, only_param=True)
                    last_epoch_opt = idx

                if last_epoch_opt is not None and (idx - last_epoch_opt > self.patience) and (dev_ckpt and idx > dev_ckpt_delay * self.n_epochs):
                    print "[{}] No model improvement after {} epochs, halting".format(self.name, idx - last_epoch_opt)
                    break

        # Conclude training
        if verbose:
            print("[{0}] Training done ({1:.2f}s)".format(self.name, time() - st))

        # If checkpointing on, load last checkpoint (i.e. best on dev set)
        if dev_ckpt and X_dev is not None and verbose and dev_score_opt > 0:
            self.load(save_dir=save_dir, only_param=True)

    def _marginals_batch(self, X):
        """Predict class based on user input"""
        self.char_model.eval()
        self.word_model.eval()

        X_w, X_c = self._preprocess_data(X, extend=False)
        sigmoid = nn.Sigmoid()

        y = np.array([])

        x = torch.from_numpy(np.arange(len(X_w)))
        data_set = data_utils.TensorDataset(x, x)
        data_loader = data_utils.DataLoader(data_set, batch_size=self.batch_size, shuffle=False)

        for x, _ in data_loader:
            x_w, x_w_mask, x_c, x_c_mask = pad_batch(X_w[x.numpy()], X_c[x.numpy()], self.max_sentence_length,
                                                     self.max_word_length)
            batch_size, max_sent, max_token = x_c.size()
            w_state_word = self.word_model.init_hidden(batch_size)
            c_state_word = self.char_model.init_hidden(batch_size)

            if self.host_device in self.gpu:
                x_w = x_w.cuda()
                x_w_mask = x_w_mask.cuda()
                x_c = x_c.cuda()
                x_c_mask = x_c_mask.cuda()
                w_state_word = (w_state_word[0].cuda(), w_state_word[1].cuda())
                c_state_word = (c_state_word[0].cuda(), c_state_word[1].cuda())

            s = None
            for i in xrange(max_sent):
                _s = self.char_model(x_c[:, i, :], x_c_mask[:, i, :], c_state_word)
                _s = _s.unsqueeze(0)
                s = _s if s is None else torch.cat((s, _s), 0)
            s = s.transpose(0, 1)
            y_pred = self.word_model(x_w, x_w_mask, s, w_state_word)
            if self.host_device in self.gpu:
                y = np.append(y, sigmoid(y_pred).data.cpu().numpy())
            else:
                y = np.append(y, sigmoid(y_pred).data.numpy())
        return y

    def save(self, model_name=None, save_dir='checkpoints', verbose=True, only_param=False):
        """Save current model"""
        model_name = model_name or self.name

        # Note: Model checkpoints need to be saved in separate directories!
        model_dir = os.path.join(save_dir, model_name)
        if not os.path.exists(model_dir):
            os.makedirs(model_dir)

        if not only_param:
            # Save model kwargs needed to rebuild model
            with open(os.path.join(model_dir, "model_kwargs.pkl"), 'wb') as f:
                dump(self.model_kwargs, f)

            if self.load_emb:
                # Save model dicts needed to rebuild model
                with open(os.path.join(model_dir, "model_dicts.pkl"), 'wb') as f:
                    dump({'char_dict': self.char_dict, 'word_dict': self.word_dict, 'char_emb': self.char_emb,
                          'word_emb': self.word_emb}, f)
            else:
                # Save model dicts needed to rebuild model
                with open(os.path.join(model_dir, "model_dicts.pkl"), 'wb') as f:
                    dump({'char_dict': self.char_dict, 'word_dict': self.word_dict}, f)

        torch.save(self.word_model, os.path.join(model_dir, model_name + '_word_model'))
        torch.save(self.char_model, os.path.join(model_dir, model_name + '_char_model'))

        if verbose:
            print("[{0}] Model saved as <{1}>, only_param={2}".format(self.name, model_name, only_param))

    def load(self, model_name=None, save_dir='checkpoints', verbose=True, only_param=False):
        """Load model from file and rebuild in new model"""
        model_name = model_name or self.name
        model_dir = os.path.join(save_dir, model_name)

        if not only_param:
            # Load model kwargs needed to rebuild model
            with open(os.path.join(model_dir, "model_kwargs.pkl"), 'rb') as f:
                model_kwargs = load(f)
                self._init_kwargs(**model_kwargs)

            if self.load_emb:
                # Save model dicts needed to rebuild model
                with open(os.path.join(model_dir, "model_dicts.pkl"), 'rb') as f:
                    d = load(f)
                    self.char_dict = d['char_dict']
                    self.word_dict = d['word_dict']
                    self.char_emb = d['char_emb']
                    self.word_emb = d['word_emb']
            else:
                # Save model dicts needed to rebuild model
                with open(os.path.join(model_dir, "model_dicts.pkl"), 'rb') as f:
                    d = load(f)
                    self.char_dict = d['char_dict']
                    self.word_dict = d['word_dict']

        self.word_model = torch.load(os.path.join(model_dir, model_name + '_word_model'))
        self.char_model = torch.load(os.path.join(model_dir, model_name + '_char_model'))

        if verbose:
            print("[{0}] Loaded model <{1}>, only_param={2}".format(self.name, model_name, only_param))