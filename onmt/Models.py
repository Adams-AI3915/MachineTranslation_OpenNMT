from __future__ import division
import torch
import torch.nn as nn
from torch.autograd import Variable
from torch.nn.utils.rnn import pad_packed_sequence as unpack
from torch.nn.utils.rnn import pack_padded_sequence as pack

import onmt
from onmt.Utils import aeq


class Encoder(nn.Module):
    """
    Encoder recurrent neural network.
    """
    def __init__(self, encoder_type, bidirectional, rnn_type,
                 num_layers, rnn_size, dropout, embeddings,
                 cnn_kernel_width):
        """
        Args:
            encoder_type (string): rnn, brnn, mean, or transformer.
            bidirectional (bool): bidirectional Encoder.
            rnn_type (string): LSTM or GRU.
            num_layers (int): number of Encoder layers.
            rnn_size (int): size of hidden states of a rnn.
            dropout (float): dropout probablity.
            embeddings (Embeddings): vocab embeddings for this Encoder.
        """
        super(Encoder, self).__init__()

        # Basic attributes.
        self.encoder_type = encoder_type
        self.num_directions = 2 if bidirectional else 1
        assert rnn_size % self.num_directions == 0
        self.num_layers = num_layers
        self.hidden_size = rnn_size // self.num_directions
        self.embeddings = embeddings

        # Build the Encoder RNN.
        if self.encoder_type == "transformer":
            self.transformer = nn.ModuleList(
                [onmt.modules.TransformerEncoderLayer(
                    self.hidden_size, dropout)
                 for i in range(self.num_layers)])
        elif self.encoder_type == "cnn":
            self.cnn = onmt.modules.ConvEncoder(
                self.embeddings.embedding_dim, self.hidden_size,
                self.num_layers, dropout, cnn_kernel_width)
        else:
            self.rnn = getattr(nn, rnn_type)(
                input_size=self.embeddings.embedding_dim,
                hidden_size=self.hidden_size,
                num_layers=self.num_layers,
                dropout=dropout,
                bidirectional=bidirectional)

    def forward(self, input, lengths=None, hidden=None):
        """
        Args:
            input (LongTensor): len x batch x nfeat
            lengths (LongTensor): batch
            hidden: Initial hidden state.
        Returns:
            hidden_t (FloatTensor): Pair of layers x batch x rnn_size - final
                                    Encoder state
            outputs (FloatTensor):  len x batch x rnn_size -  Memory bank
        """
        # CHECKS
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            n_batch_, = lengths.size()
            aeq(n_batch, n_batch_)
        # END CHECKS

        emb = self.embeddings(input)
        s_len, n_batch, emb_dim = emb.size()

        if self.encoder_type == "mean":
            # No RNN, just take mean as final state.
            mean = emb.mean(0).expand(self.num_layers, n_batch, emb_dim)
            return (mean, mean), emb

        elif self.encoder_type == "transformer":
            # Self-attention tranformer.
            out = emb.transpose(0, 1).contiguous()
            words = input[:, :, 0].transpose(0, 1)
            # CHECKS
            out_batch, out_len, _ = out.size()
            w_batch, w_len = words.size()
            aeq(out_batch, w_batch)
            aeq(out_len, w_len)
            # END CHECKS

            # Make mask.
            padding_idx = self.embeddings.word_padding_idx
            mask = words.data.eq(padding_idx).unsqueeze(1) \
                .expand(w_batch, w_len, w_len)

            # Run the forward pass of every layer of the tranformer.
            for i in range(self.num_layers):
                out = self.transformer[i](out, mask)

            return Variable(emb.data), out.transpose(0, 1).contiguous()
        elif self.encoder_type == "cnn":
            out = emb.transpose(0, 1).contiguous()
            out, emb_remap = self.cnn(out)
            return emb_remap.transpose(0, 1).contiguous(),\
                out.transpose(0, 1).contiguous()
        else:
            # Standard RNN encoder.
            packed_emb = emb
            if lengths is not None:
                # Lengths data is wrapped inside a Variable.
                lengths = lengths.view(-1).tolist()
                packed_emb = pack(emb, lengths)
            outputs, hidden_t = self.rnn(packed_emb, hidden)
            if lengths:
                outputs = unpack(outputs)[0]
            return hidden_t, outputs


class EncoderBase(nn.Module):
    """
    EncoderBase class for sharing code among various *Encoder.
    """
    def _check_args(self, input, lengths=None, hidden=None):
        s_len, n_batch, n_feats = input.size()
        if lengths is not None:
            n_batch_, = lengths.size()
            aeq(n_batch, n_batch_)

    def forward(self, input, lengths=None, hidden=None):
        """
        Args:
            input (LongTensor): len x batch x nfeat.
            lengths (LongTensor): batch
            hidden: Initial hidden state.
        Returns:
            hidden_t (Variable): Pair of layers x batch x rnn_size - final
                                    Encoder state
            outputs (FloatTensor):  len x batch x rnn_size -  Memory bank
        """
        raise NotImplementedError


class MeanEncoder(EncoderBase):
    """ A trivial encoder without RNN, just takes mean of as final state. """
    def __init__(self, num_layers, embeddings):
        super(MeanEncoder, self).__init__()
        self.num_layers = num_layers
        self.embeddings = embeddings

    def forward(self, input, lengths=None, hidden=None):
        """ See EncoderBase.forward() for description of args and returns. """
        self._check_args(input, lengths, hidden)

        emb = self.embeddings(input)
        s_len, batch, emb_dim = emb.size()
        mean = emb.mean(0).expand(self.num_layers, batch, emb_dim)
        return (mean, mean), emb


class RNNEncoder(EncoderBase):
    """ The standard RNN encoder. """
    def __init__(self, rnn_type, bidirectional, num_layers,
                 hidden_size, dropout, embeddings):
        super(RNNEncoder, self).__init__()

        num_directions = 2 if bidirectional else 1
        assert hidden_size % num_directions == 0
        hidden_size = hidden_size // num_directions
        self.embeddings = embeddings
        self.rnn = getattr(nn, rnn_type)(
                input_size=embeddings.embedding_dim,
                hidden_size=hidden_size,
                num_layers=num_layers,
                dropout=dropout,
                bidirectional=bidirectional)

    def forward(self, input, lengths=None, hidden=None):
        """ See EncoderBase.forward() for description of args and returns."""
        self._check_args(input, lengths, hidden)

        emb = self.embeddings(input)
        s_len, batch, emb_dim = emb.size()
        packed_emb = emb
        if lengths is not None:
            # Lengths data is wrapped inside a Variable.
            lengths = lengths.view(-1).tolist()
            packed_emb = pack(emb, lengths)
        outputs, hidden_t = self.rnn(packed_emb, hidden)
        if lengths:
            outputs = unpack(outputs)[0]
        return hidden_t, outputs


class RNNDecoderBase(nn.Module):
    """
    RNN Decoder base class.
    """
    def __init__(self, rnn_type, bidirectional_encoder, num_layers,
                 hidden_size, attn_type, coverage_attn, context_gate,
                 copy_attn, dropout, embeddings):
        """
        See make_decoder() comment for arguments description.
        """
        super(RNNDecoderBase, self).__init__()

        # Basic attributes.
        self.decoder_type = 'rnn'
        self.bidirectional_encoder = bidirectional_encoder
        self.num_layers = num_layers
        self.hidden_size = hidden_size
        self.embeddings = embeddings
        self.dropout = nn.Dropout(dropout)

        # Build the RNN.
        self.rnn = self._build_rnn(rnn_type, self._input_size, hidden_size,
                                   num_layers, dropout)

        # Set up the context gate.
        self.context_gate = None
        if context_gate is not None:
            self.context_gate = onmt.modules.ContextGateFactory(
                context_gate, self._input_size,
                hidden_size, hidden_size, hidden_size
            )

        # Set up the standard attention.
        self._coverage = coverage_attn
        self.attn = onmt.modules.GlobalAttention(
            hidden_size, coverage=coverage_attn,
            attn_type=attn_type
        )

        # Set up a separated copy attention layer, if needed.
        self._copy = False
        if copy_attn:
            self.copy_attn = onmt.modules.GlobalAttention(
                hidden_size, attn_type=attn_type
            )
            self._copy = True

    def forward(self, input, context, state):
        """
        Forward through the decoder.
        Args:
            input (LongTensor): a sequence of input tokens tensors
                                of size (len x batch x nfeats).
            context (FloatTensor): output(tensor sequence) from the Encoder
                        RNN of size (src_len x batch x hidden_size).
            state (FloatTensor): hidden state from the Encoder RNN for
                                 initializing the decoder.
        Returns:
            outputs (FloatTensor): a Tensor sequence of output from the Decoder
                                   of shape (len x batch x hidden_size).
            state (FloatTensor): final hidden state from the Decoder.
            attns (dict of (str, FloatTensor)): a dictionary of different
                                type of attention Tensor from the Decoder
                                of shape (src_len x batch).
        """
        # Args Check
        assert isinstance(state, RNNDecoderState)
        input_len, input_batch, _ = input.size()
        contxt_len, contxt_batch, _ = context.size()
        aeq(input_batch, contxt_batch)
        # END Args Check

        # Run the forward pass of the RNN.
        hidden, outputs, attns, coverage = \
            self._run_forward_pass(input, context, state)

        # Update the state with the result.
        final_output = outputs[-1]
        state.update_state(hidden, final_output.unsqueeze(0),
                           coverage.unsqueeze(0)
                           if coverage is not None else None)

        # Concatenates sequence of tensors along a new dimension.
        outputs = torch.stack(outputs)
        for k in attns:
            attns[k] = torch.stack(attns[k])

        return outputs, state, attns

    def _fix_enc_hidden(self, h):
        """
        The encoder hidden is  (layers*directions) x batch x dim.
        We need to convert it to layers x batch x (directions*dim).
        """
        if self.bidirectional_encoder:
            h = torch.cat([h[0:h.size(0):2], h[1:h.size(0):2]], 2)
        return h

    def init_decoder_state(self, src, context, enc_hidden):
        if isinstance(enc_hidden, tuple):  # GRU
            return RNNDecoderState(context, self.hidden_size,
                                   tuple([self._fix_enc_hidden(enc_hidden[i])
                                         for i in range(len(enc_hidden))]))
        else:  # LSTM
            return RNNDecoderState(context, self.hidden_size,
                                   self._fix_enc_hidden(enc_hidden))


class StdRNNDecoder(RNNDecoderBase):
    """
    Stardard RNN Decoder, with Attention.
    Currently no 'coverage_attn' and 'copy_attn' support.
    """
    def _run_forward_pass(self, input, context, state):
        """
        Private helper for running the specific RNN forward pass.
        Must be overriden by all subclasses.
        Args:
            input (LongTensor): a sequence of input tokens tensors
                                of size (len x batch x nfeats).
            context (FloatTensor): output(tensor sequence) from the Encoder
                        RNN of size (src_len x batch x hidden_size).
            state (FloatTensor): hidden state from the Encoder RNN for
                                 initializing the decoder.
        Returns:
            hidden (Variable): final hidden state from the Decoder.
            outputs ([FloatTensor]): an array of output of every time
                                     step from the Decoder.
            attns (dict of (str, [FloatTensor]): a dictionary of different
                            type of attention Tensor array of every time
                            step from the Decoder.
            coverage (FloatTensor, optional): coverage from the Decoder.
        """
        assert not self._copy  # TODO, no support yet.
        assert not self._coverage  # TODO, no support yet.

        # Initialize local and return variables.
        outputs = []
        attns = {"std": []}
        coverage = None

        emb = self.embeddings(input)

        # Run the forward pass of the RNN.
        rnn_output, hidden = self.rnn(emb, state.hidden)
        # Result Check
        input_len, input_batch, _ = input.size()
        output_len, output_batch, _ = rnn_output.size()
        aeq(input_len, output_len)
        aeq(input_batch, output_batch)
        # END Result Check

        # Calculate the attention.
        attn_outputs, attn_scores = self.attn(
            rnn_output.transpose(0, 1).contiguous(),  # (output_len, batch, d)
            context.transpose(0, 1)                   # (contxt_len, batch, d)
        )
        attns["std"] = attn_scores

        # Calculate the context gate.
        if self.context_gate is not None:
            outputs = self.context_gate(
                emb.view(-1, emb.size(2)),
                rnn_output.view(-1, rnn_output.size(2)),
                attn_outputs.view(-1, attn_outputs.size(2))
            )
            outputs = outputs.view(input_len, input_batch, self.hidden_size)
            outputs = self.dropout(outputs)
        else:
            outputs = self.dropout(attn_outputs)    # (input_len, batch, d)

        # Return result.
        return hidden, outputs, attns, coverage

    def _build_rnn(self, rnn_type, input_size,
                   hidden_size, num_layers, dropout):
        """
        Private helper for building standard Decoder RNN.
        """
        return getattr(nn, rnn_type)(
            input_size, hidden_size,
            num_layers=num_layers,
            dropout=dropout)

    @property
    def _input_size(self):
        """
        Private helper returning the number of expected features.
        """
        return self.embeddings.embedding_dim


class InputFeedRNNDecoder(RNNDecoderBase):
    """
    Stardard RNN Decoder, with Input Feed and Attention.
    """
    def _run_forward_pass(self, input, context, state):
        """
        See StdRNNDecoder._run_forward_pass() for description
        of arguments and return values.
        """
        # Additional args check.
        output = state.input_feed.squeeze(0)
        output_batch, _ = output.size()
        input_len, input_batch, _ = input.size()
        aeq(input_batch, output_batch)
        # END Additional args check.

        # Initialize local and return variables.
        outputs = []
        attns = {"std": []}
        if self._copy:
            attns["copy"] = []
        if self._coverage:
            attns["coverage"] = []

        emb = self.embeddings(input)
        assert emb.dim() == 3  # len x batch x embedding_dim

        hidden = state.hidden
        coverage = state.coverage.squeeze(0) \
            if state.coverage is not None else None

        # Input feed concatenates hidden state with
        # input at every time step.
        for i, emb_t in enumerate(emb.split(1)):
            emb_t = emb_t.squeeze(0)
            emb_t = torch.cat([emb_t, output], 1)

            rnn_output, hidden = self.rnn(emb_t, hidden)
            attn_output, attn = self.attn(rnn_output,
                                          context.transpose(0, 1))
            if self.context_gate is not None:
                output = self.context_gate(
                    emb_t, rnn_output, attn_output
                )
                output = self.dropout(output)
            else:
                output = self.dropout(attn_output)
            outputs += [output]
            attns["std"] += [attn]

            # Update the coverage attention.
            if self._coverage:
                coverage = coverage + attn \
                    if coverage is not None else attn
                attns["coverage"] += [coverage]

            # Run the forward pass of the copy attention layer.
            if self._copy:
                _, copy_attn = self.copy_attn(output,
                                              context.transpose(0, 1))
                attns["copy"] += [copy_attn]

        # Return result.
        return hidden, outputs, attns, coverage

    def _build_rnn(self, rnn_type, input_size,
                   hidden_size, num_layers, dropout):
        if rnn_type == "LSTM":
            stacked_cell = onmt.modules.StackedLSTM
        else:
            stacked_cell = onmt.modules.StackedGRU
        return stacked_cell(num_layers, input_size,
                            hidden_size, dropout)

    @property
    def _input_size(self):
        """
        Using input feed by concatenating input with attention vectors.
        """
        return self.embeddings.embedding_dim + self.hidden_size


class NMTModel(nn.Module):
    """
    The Encoder + Decoder Neural Machine Translation Model.
    """
    def __init__(self, encoder, decoder, multigpu=False):
        """
        Args:
            encoder(Encoder): the Encoder.
            decoder(*Decoder): the various *Decoder.
            multigpu(bool): run parellel on multi-GPU?
        """
        self.multigpu = multigpu
        super(NMTModel, self).__init__()
        self.encoder = encoder
        self.decoder = decoder

    def forward(self, src, tgt, lengths, dec_state=None):
        """
        Args:
            src(FloatTensor): a sequence of source tensors with
                    optional feature tensors of size (len x batch).
            tgt(FloatTensor): a sequence of target tensors with
                    optional feature tensors of size (len x batch).
            lengths([int]): an array of the src length.
            dec_state: A decoder state object
        Returns:
            outputs (FloatTensor): (len x batch x hidden_size): Decoder outputs
            attns (FloatTensor): Dictionary of (src_len x batch)
            dec_hidden (FloatTensor): tuple (1 x batch x hidden_size)
                                      Init hidden state
        """
        src = src
        tgt = tgt[:-1]  # exclude last target from inputs
        enc_hidden, context = self.encoder(src, lengths)
        enc_state = self.decoder.init_decoder_state(src, context, enc_hidden)
        out, dec_state, attns = self.decoder(tgt, context,
                                             enc_state if dec_state is None
                                             else dec_state)
        if self.multigpu:
            # Not yet supported on multi-gpu
            dec_state = None
            attns = None
        return out, attns, dec_state


class DecoderState(object):
    """
    DecoderState is a base class for models, used during translation
    for storing translation states.
    """
    def detach(self):
        """
        Detaches all Variables from the graph
        that created it, making it a leaf.
        """
        for h in self._all:
            if h is not None:
                h.detach_()

    def beam_update(self, idx, positions, beam_size):
        """ Update when beam advances. """
        for e in self._all:
            a, br, d = e.size()
            sentStates = e.view(a, beam_size, br // beam_size, d)[:, :, idx]
            sentStates.data.copy_(
                sentStates.data.index_select(1, positions))


class RNNDecoderState(DecoderState):
    def __init__(self, context, hidden_size, rnnstate):
        """
        Args:
            context (FloatTensor): output from the Encoder of size
                                   len x batch x rnn_size.
            hidden_size (int): the size of hidden layer of the Decoder.
            rnnstate (Variable): final hidden state from the Encoder.
                transformed to shape: layers x batch x (directions*dim).
            input_feed (FloatTensor): output from last layer of the Decoder.
            coverage (FloatTensor): coverage output from the Decoder.
        """
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.coverage = None

        # Init the input feed.
        batch_size = context.size(1)
        h_size = (batch_size, hidden_size)
        self.input_feed = Variable(context.data.new(*h_size).zero_(),
                                   requires_grad=False).unsqueeze(0)

    @property
    def _all(self):
        return self.hidden + (self.input_feed,)

    def update_state(self, rnnstate, input_feed, coverage):
        if not isinstance(rnnstate, tuple):
            self.hidden = (rnnstate,)
        else:
            self.hidden = rnnstate
        self.input_feed = input_feed
        self.coverage = coverage

    def repeat_beam_size_times(self, beam_size):
        """ Repeat beam_size times along batch dimension. """
        vars = [Variable(e.data.repeat(1, beam_size, 1), volatile=True)
                for e in self._all]
        self.hidden = tuple(vars[:-1])
        self.input_feed = vars[-1]
