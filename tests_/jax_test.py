import os
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"
os.environ["XLA_PYTHON_CLIENT_ALLOCATOR"] = "platform"
import jax
from scipy.stats import pearsonr
print('DEVICES:', jax.devices())
import jax.numpy as jnp
from jax_llama import model as jax_model
from jax_llama import config
from llama import model
import torch
import numpy as np
from fairscale.nn.model_parallel.initialize import initialize_model_parallel
from typing import Tuple, List, Tuple, Optional
import os
from flax.core.frozen_dict import freeze
from dataclasses import dataclass
import functools
from llama.generation import Llama as torch_load
from jax_example import load as jax_load
from flax.linen import make_causal_mask
from jax_llama.llama2_tokenizer import Tokenizer as LLaMA2Tokenizer
from jax_llama.llama3_tokenizer import Tokenizer as LLaMA3Tokenizer
from llama.tokenizer import Tokenizer
from jax_llama.partition import with_named_sharding_constraint
from jax.sharding import PartitionSpec as P
import fire


@dataclass
class ModelArgs:
    dim: int = 64 #4096
    n_layers: int = 1
    n_heads: int = 32
    vocab_size: int = 128 #128256
    n_kv_heads: Optional[int] = 8
    multiple_of: int = 256
    ffn_dim_multiplier: Optional[float] = None
    norm_eps: float = 1e-5
    rope_theta: float = 500000.0

    max_batch_size: int = 1
    max_seq_len: int = 64  # official max

    def transformers_config(self) -> config.LLaMAConfig:
        #intermediate_size = 14336  # ← taken directly from HF config
        hidden_dim = int(2 * (4 * self.dim) / 3)
        hidden_dim = self.multiple_of * ((hidden_dim + self.multiple_of - 1) // self.multiple_of)

        return config.LLaMAConfig(
            vocab_size=self.vocab_size,
            hidden_size=self.dim,
            #intermediate_size=intermediate_size,
            intermediate_size=hidden_dim,
            num_hidden_layers=self.n_layers,
            num_attention_heads=self.n_heads,
            num_key_value_heads=self.n_kv_heads,
            max_position_embeddings=self.max_seq_len,
            rms_norm_eps=self.norm_eps,
            rope_theta=self.rope_theta,
            rope_scaling={
                "factor": 8.0,
                "high_freq_factor": 4.0,
                "low_freq_factor": 1.0,
                "original_max_position_embeddings": 8192,
                "rope_type": "llama3"
            }
        )

def test_RMSNorm(args: ModelArgs, total_tests: int, atol: float) -> np.ndarray:
    errs = []
    pccs = []
    for test_n in range(total_tests):
        x = np.random.randn(args.max_batch_size, args.dim).astype(np.float32)

        jax_rms_norm = jax_model.RMSNorm(args.dim, eps=args.norm_eps)
        jax_params = jax_rms_norm.init(jax.random.PRNGKey(0), jnp.ones(args.dim, dtype=jnp.float32))['params']
        jax_output = jax_rms_norm.apply({'params': jax_params}, jnp.asarray(x))
        jax_output = np.asarray(jax_output)

        torch_rms_norm = model.RMSNorm(args.dim, eps=args.norm_eps)
        torch_output = torch_rms_norm(torch.tensor(x))
        torch_output = torch_output.detach().numpy()

        assert np.allclose(jax_output, torch_output, atol=atol), f"RMSNorm test {test_n} failed"
        errs.append(np.max(np.abs(jax_output - torch_output)))

        flat_jax = jax_output.flatten()
        flat_torch = torch_output.flatten()
        pcc_value, _ = pearsonr(flat_jax, flat_torch)
        pccs.append(pcc_value)

        print(f"[RMSNorm test {test_n}] Max Error: {errs[-1]:.6f}, PCC: {pcc_value:.6f}")
    return np.asarray(errs, dtype=np.float32)

def test_precompute_freqs_cis(args: ModelArgs, atol: float) -> float:
    jax_freqs_cis = jax_model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)
    jax_freqs_cis = np.asarray(jax_freqs_cis)

    torch_freqs_cis = model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)
    torch_freqs_cis = torch_freqs_cis.detach().numpy()

    assert np.allclose(jax_freqs_cis, torch_freqs_cis, atol=atol), f"precompute_freqs_cis test failed"
    return np.max(np.abs(jax_freqs_cis - torch_freqs_cis))

def test_apply_roary_emb(args: ModelArgs, total_tests: int, atol: float) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    errs0, errs1 = [], []
    pccs0, pccs1 = [], []
    
    for test_n in range(total_tests):
        xq = np.random.randn(args.max_batch_size, args.max_seq_len, args.n_heads, args.dim // args.n_heads).astype(np.float32)
        xk = np.random.randn(args.max_batch_size, args.max_seq_len, args.n_heads, args.dim // args.n_heads).astype(np.float32)

        jax_freqs_cis = jax_model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)
        jax_output = jax_model.apply_rotary_emb(jnp.asarray(xq), jnp.asarray(xk), jax_freqs_cis[None])
        jax_output = (np.asarray(jax_output[0]), np.asarray(jax_output[1]))

        torch_freqs_cis = model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)
        torch_output = model.apply_rotary_emb(torch.tensor(xq), torch.tensor(xk), torch_freqs_cis)
        torch_output = (torch_output[0].detach().numpy(), torch_output[1].detach().numpy())

        assert np.allclose(jax_output[0], torch_output[0], atol=atol) and \
               np.allclose(jax_output[1], torch_output[1], atol=atol), f"apply_rotary_emb test {test_n} failed"

        # Max absolute error
        errs0.append(np.max(np.abs(jax_output[0] - torch_output[0])))
        errs1.append(np.max(np.abs(jax_output[1] - torch_output[1])))

        # PCC
        pcc0, _ = pearsonr(jax_output[0].flatten(), torch_output[0].flatten())
        pcc1, _ = pearsonr(jax_output[1].flatten(), torch_output[1].flatten())
        pccs0.append(pcc0)
        pccs1.append(pcc1)
        print("pcc0:", pcc0)
        print("pcc1:", pcc1)

    return (
        np.asarray(errs0, dtype=np.float32),
        np.asarray(errs1, dtype=np.float32),
        np.asarray(pccs0, dtype=np.float32),
        np.asarray(pccs1, dtype=np.float32)
    )

def test_Attention(args: ModelArgs, total_tests: int, atol: float) -> Tuple[np.ndarray, np.ndarray]:
    kv_heads = args.n_kv_heads if args.n_kv_heads is not None else args.n_heads
    errs = []
    pccs = []
    for test_n in range(total_tests):
        x = np.random.randn(args.max_batch_size, args.max_seq_len, args.dim).astype(np.float32)
        wq = np.random.randn(args.dim, args.dim).astype(np.float32)
        wk = np.random.randn(args.dim, args.dim // (args.n_heads // kv_heads)).astype(np.float32)
        wv = np.random.randn(args.dim, args.dim // (args.n_heads // kv_heads)).astype(np.float32)
        wo = np.random.randn(args.dim, args.dim).astype(np.float32)

        jax_attention = jax_model.FlaxLLaMAAttention(args.transformers_config(), precision='highest')
        jax_params = freeze({
            'wq': {'kernel': jnp.asarray(wq)},
            'wk': {'kernel': jnp.asarray(wk)},
            'wv': {'kernel': jnp.asarray(wv)},
            'wo': {'kernel': jnp.asarray(wo)},
        })
        jax_output = jax_attention.apply(
            {'params': jax_params},
            jnp.asarray(x),
            jnp.ones((args.max_batch_size, args.max_seq_len), dtype=np.int32),
            jnp.broadcast_to(jnp.arange(args.max_seq_len, dtype=np.int32)[None, :], (args.max_batch_size, args.max_seq_len)),
        )[0]
        jax_output = np.asarray(jax_output)

        torch_freqs_cis = model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)

        torch_attention = model.Attention(
            model.ModelArgs(
                n_heads=args.n_heads,
                n_kv_heads=kv_heads,
                dim=args.dim,
                max_batch_size=args.max_batch_size,
                max_seq_len=args.max_seq_len,
                rope_theta=args.rope_theta,
            ),
        )

        torch_attention.load_state_dict({
            "wo.weight": torch.tensor(wo.transpose()),
            "wq.weight": torch.tensor(wq.transpose()),
            "wv.weight": torch.tensor(wv.transpose()),
            "wk.weight": torch.tensor(wk.transpose()),
        })

        torch_output = torch_attention(
            torch.tensor(x),
            0,
            torch_freqs_cis,
            torch.where(torch.tensor(np.asarray(make_causal_mask(jnp.ones((1, args.max_seq_len), dtype="bool"), dtype="bool"))) == False, float(jnp.finfo(jnp.float32).min), 0.0),
        )
        torch_output = torch_output.detach().numpy()

        # Compute max error
        errs.append(np.max(np.abs(jax_output - torch_output)))

        # Compute PCC
        pcc, _ = pearsonr(jax_output.flatten(), torch_output.flatten())
        pccs.append(pcc)
        print("pcc", pcc)
        assert np.allclose(jax_output, torch_output, atol=atol), f"Attention test {test_n} failed"

    return np.asarray(errs, dtype=np.float32), np.asarray(pccs, dtype=np.float32)

def test_feedForward(args: ModelArgs, total_tests: int, atol: float) -> Tuple[np.ndarray, np.ndarray]:
    errs = []
    pccs = []
    for test_n in range(total_tests):
        transformers_config = args.transformers_config()
        x = np.random.randn(args.max_batch_size, args.max_seq_len, args.dim).astype(np.float32)
        w1 = np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)
        w2 = np.random.randn(transformers_config.intermediate_size, args.dim).astype(np.float32)
        w3 = np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)

        jax_mlp = jax_model.FlaxLLaMAMLP(transformers_config, precision='highest')
        jax_params = freeze({
            'w1': {'kernel': jnp.asarray(w1)},
            'w2': {'kernel': jnp.asarray(w2)},
            'w3': {'kernel': jnp.asarray(w3)},
        })
        jax_output = jax_mlp.apply({'params': jax_params}, jnp.asarray(x))
        jax_output = np.asarray(jax_output)

        torch_mlp = model.FeedForward(
            dim=args.dim,
            hidden_dim=args.dim * 4,
            multiple_of=args.multiple_of,
            ffn_dim_multiplier=args.ffn_dim_multiplier,
        )
        torch_mlp.load_state_dict({
            "w1.weight": torch.tensor(w1.transpose()),
            "w2.weight": torch.tensor(w2.transpose()),
            "w3.weight": torch.tensor(w3.transpose()),
        })
        torch_output = torch_mlp(torch.tensor(x))
        torch_output = torch_output.detach().numpy()

        # Max error
        errs.append(np.max(np.abs(jax_output - torch_output)))

        # PCC
        pcc, _ = pearsonr(jax_output.flatten(), torch_output.flatten())
        pccs.append(pcc)
        print(f"[FeedForward test {test_n}] Max Error: {errs[-1]:.6f}, PCC: {pcc:.6f}")

        assert np.allclose(jax_output, torch_output, atol=atol), f"FeedForward test {test_n} failed"

    return np.asarray(errs, dtype=np.float32), np.asarray(pccs, dtype=np.float32)

def test_TransformerBlock(args: ModelArgs, total_tests: int, atol: float) -> np.ndarray:
    kv_heads = args.n_kv_heads if args.n_kv_heads is not None else args.n_heads
    errs = []
    pccs = []
    for test_n in range(total_tests):
        transformers_config = args.transformers_config()
        x = np.random.randn(args.max_batch_size, args.max_seq_len, args.dim).astype(np.float32)
        wq = np.random.randn(args.dim, args.dim).astype(np.float32)
        wk = np.random.randn(args.dim, args.dim//(args.n_heads//kv_heads)).astype(np.float32)
        wv = np.random.randn(args.dim, args.dim//(args.n_heads//kv_heads)).astype(np.float32)
        wo = np.random.randn(args.dim, args.dim).astype(np.float32)
        w1 = np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)
        w2 = np.random.randn(transformers_config.intermediate_size, args.dim).astype(np.float32)
        w3 = np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)
        attention_norm_scale = np.random.randn(args.dim).astype(np.float32)
        ffn_norm_scale = np.random.randn(args.dim).astype(np.float32)

        jax_transformer_block = jax_model.FlaxLLaMABlock(transformers_config, precision='highest')
        jax_params = freeze({
            'attention': {
                'wq': {'kernel': jnp.asarray(wq)}, 
                'wk': {'kernel': jnp.asarray(wk)}, 
                'wv': {'kernel': jnp.asarray(wv)}, 
                'wo': {'kernel': jnp.asarray(wo)}, 
            }, 
            'feed_forward': {
                'w1': {'kernel': jnp.asarray(w1)}, 
                'w2': {'kernel': jnp.asarray(w2)}, 
                'w3': {'kernel': jnp.asarray(w3)}, 
            }, 
            'attention_norm': {'kernel': jnp.asarray(attention_norm_scale)}, 
            'ffn_norm': {'kernel': jnp.asarray(ffn_norm_scale)}, 
        })
        # get output
        jax_output = jax_transformer_block.apply(
            {'params': jax_params}, 
            jnp.asarray(x), 
            jnp.ones((args.max_batch_size, args.max_seq_len), dtype=np.int32), 
            jnp.broadcast_to(jnp.arange(args.max_seq_len, dtype=np.int32)[None, :], (args.max_batch_size, args.max_seq_len)), 
        )[0]
        jax_output = np.asarray(jax_output)

        torch_freqs_cis = model.precompute_freqs_cis(args.dim // args.n_heads, args.max_seq_len)

        torch_transformer_block = model.TransformerBlock(
            0, 
            model.ModelArgs(
                dim=args.dim, 
                n_heads=args.n_heads, 
                n_kv_heads=kv_heads,
                multiple_of=args.multiple_of, 
                ffn_dim_multiplier=args.ffn_dim_multiplier,
                norm_eps=args.norm_eps, 
                rope_theta=args.rope_theta,
                max_batch_size=args.max_batch_size, 
                max_seq_len=args.max_seq_len, 
            ), 
        )

        torch_transformer_block.load_state_dict({
            "attention.wo.weight": torch.tensor(wo.transpose()), 
            "attention.wq.weight": torch.tensor(wq.transpose()), 
            "attention.wv.weight": torch.tensor(wv.transpose()), 
            "attention.wk.weight": torch.tensor(wk.transpose()), 
            "feed_forward.w1.weight": torch.tensor(w1.transpose()), 
            "feed_forward.w2.weight": torch.tensor(w2.transpose()), 
            "feed_forward.w3.weight": torch.tensor(w3.transpose()), 
            "attention_norm.weight": torch.tensor(attention_norm_scale), 
            "ffn_norm.weight": torch.tensor(ffn_norm_scale), 
        }) # load weights, have to transpose because pytorch linear layers are reversed from Jax.
        torch_output = torch_transformer_block(
            torch.tensor(x), 
            0, 
            torch_freqs_cis, 
            torch.where(torch.tensor(np.asarray(make_causal_mask(jnp.ones((1, args.max_seq_len), dtype="bool"), dtype="bool"))) == False, float(jnp.finfo(jnp.float32).min), 0.0), 
        )
        torch_output = torch_output.detach().numpy()

        err = np.max(np.abs(jax_output - torch_output))
        pcc, _ = pearsonr(jax_output.flatten(), torch_output.flatten())

        print(f"[TransformerBlock test {test_n}] Max Error: {err:.6f}, PCC: {pcc:.6f}")
        assert np.allclose(jax_output, torch_output, atol=atol), f"TransformerBlock test {test_n} failed"

        errs.append(err)
        pccs.append(pcc)

    return np.asarray(errs, dtype=np.float32), np.asarray(pccs, dtype=np.float32)

def test_Transformer(args: ModelArgs, total_tests: int, atol: float) -> np.ndarray:
    kv_heads = args.n_kv_heads if args.n_kv_heads is not None else args.n_heads
    errs = []
    for test_n in range(total_tests):
        transformers_config = args.transformers_config()
        x = np.random.randint(low=0, high=args.vocab_size, size=(args.max_batch_size, args.max_seq_len), dtype=np.int32)
        layer_weights = [
            {
                'attention': {
                    'wq': {'kernel': np.random.randn(args.dim, args.dim).astype(np.float32)}, 
                    'wk': {'kernel': np.random.randn(args.dim, args.dim//(args.n_heads//kv_heads)).astype(np.float32)}, 
                    'wv': {'kernel': np.random.randn(args.dim, args.dim//(args.n_heads//kv_heads)).astype(np.float32)}, 
                    'wo': {'kernel': np.random.randn(args.dim, args.dim).astype(np.float32)}, 
                }, 
                'feed_forward': {
                    'w1': {'kernel': np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)}, 
                    'w2': {'kernel': np.random.randn(transformers_config.intermediate_size, args.dim).astype(np.float32)}, 
                    'w3': {'kernel': np.random.randn(args.dim, transformers_config.intermediate_size).astype(np.float32)}, 
                }, 
                'attention_norm': {'kernel': np.random.randn(args.dim).astype(np.float32)}, 
                'ffn_norm': {'kernel': np.random.randn(args.dim).astype(np.float32)}, 
            } for _ in range(args.n_layers)
        ]
        tok_embeddings = np.random.randn(args.vocab_size, args.dim).astype(np.float32)
        norm = np.random.randn(args.dim).astype(np.float32)
        output = np.random.randn(args.dim, args.vocab_size).astype(np.float32)

        jax_transformer = jax_model.FlaxLLaMAForCausalLMModule(transformers_config, precision='highest')
        jax_params = freeze({
            'transformer': {
                'wte': {'embedding': jnp.asarray(tok_embeddings)}, 
                'ln_f': {'kernel': jnp.asarray(norm)}, 
                'h': {'%d' % (i): layer_weights[i] for i in range(args.n_layers)}, 
            }, 
            'lm_head': {'kernel': jnp.asarray(output)}, 
        })
        jax_output = jax_transformer.apply(
            {'params': jax_params}, 
            jnp.asarray(x), 
            jnp.ones((args.max_batch_size, args.max_seq_len), dtype=np.int32), 
            jnp.broadcast_to(jnp.arange(args.max_seq_len, dtype=np.int32)[None, :], (args.max_batch_size, args.max_seq_len)), 
        ).logits[:, -1, :]
        jax_output = np.asarray(jax_output)


        torch_transformer = model.Transformer(
            model.ModelArgs(
                vocab_size=args.vocab_size, 
                n_layers=args.n_layers, 
                dim=args.dim, 
                n_heads=args.n_heads, 
                n_kv_heads=kv_heads,
                multiple_of=args.multiple_of, 
                ffn_dim_multiplier=args.ffn_dim_multiplier,
                norm_eps=args.norm_eps, 
                rope_theta=args.rope_theta,
                max_batch_size=args.max_batch_size, 
                max_seq_len=args.max_seq_len, 
            ), 
        )

        torch_layer_weight = lambda i: {
            "layers.%d.attention.wo.weight" % (i): torch.tensor(layer_weights[i]['attention']['wo']['kernel'].transpose()), 
            "layers.%d.attention.wq.weight" % (i): torch.tensor(layer_weights[i]['attention']['wq']['kernel'].transpose()), 
            "layers.%d.attention.wv.weight" % (i): torch.tensor(layer_weights[i]['attention']['wv']['kernel'].transpose()), 
            "layers.%d.attention.wk.weight" % (i): torch.tensor(layer_weights[i]['attention']['wk']['kernel'].transpose()), 
            "layers.%d.feed_forward.w1.weight" % (i): torch.tensor(layer_weights[i]['feed_forward']['w1']['kernel'].transpose()), 
            "layers.%d.feed_forward.w2.weight" % (i): torch.tensor(layer_weights[i]['feed_forward']['w2']['kernel'].transpose()), 
            "layers.%d.feed_forward.w3.weight" % (i): torch.tensor(layer_weights[i]['feed_forward']['w3']['kernel'].transpose()), 
            "layers.%d.attention_norm.weight" % (i): torch.tensor(layer_weights[i]['attention_norm']['kernel']), 
            "layers.%d.ffn_norm.weight" % (i): torch.tensor(layer_weights[i]['ffn_norm']['kernel']), 
        }
        torch_transformer.load_state_dict({
            "tok_embeddings.weight": torch.tensor(tok_embeddings), 
            "norm.weight": torch.tensor(norm), 
            "output.weight": torch.tensor(output.T), #TODO CHANGED  FROM .TRANSPOSE()
            **functools.reduce(lambda x, y: {**x, **y}, [torch_layer_weight(i) for i in range(args.n_layers)]), 
        }) # load weights
        torch_output = torch_transformer(torch.tensor(x), 0)[:, -1, :]
        torch_output = torch_output.detach().numpy()

        #errs.append(np.max(np.abs(jax_output - torch_output)))
        #assert np.allclose(jax_output, torch_output, atol=atol), f"Transformer test {test_n} failed"
        pcc, _ = pearsonr(jax_output.flatten(), torch_output.flatten())
        print("pcc", pcc)
        diff = np.abs(jax_output - torch_output)
        print(f"[Transformer test {test_n}] Max: {np.max(diff)}, Mean: {np.mean(diff)}, Median: {np.median(diff)}")
        if not np.allclose(jax_output, torch_output, atol=atol):
            print(f"[Transformer test {test_n}] FAILED")
        errs.append(np.max(diff))

    return np.asarray(errs, dtype=np.float32)

def test_Tokenizer(tokenizer_path: str, test_strs: List[str]) -> None:

    jax_tokenizer = LLaMA3Tokenizer(tokenizer_path)
    torch_tokenizer = Tokenizer(tokenizer_path)
    for str_ in test_strs:
        jax_tokens = jax_tokenizer.encode(str_, bos=True, eos=False)
        torch_tokens = torch_tokenizer.encode(str_, bos=True, eos=False)
        assert jax_tokens == torch_tokens, f"Tokenizer test failed for string: {str_}"
        assert jax_tokenizer.decode(jax_tokens) == torch_tokenizer.decode(torch_tokens), f"Tokenizer test failed for string: {str_}"

def test_ModelLogits(ckpt_dir: str, tokenizer_path: str, test_strs: List[str], atol: float) -> Optional[float]:
    assert torch.cuda.is_available(), "CUDA is not available."
    assert jax.lib.xla_bridge.get_backend().platform == "gpu"

    # load jax model
    jax_generator = jax_load(
        ckpt_dir,
        tokenizer_path,
        is_llama3 =True,
        max_seq_length=8192,
        precision='highest',
    )
    jax_model, jax_params = jax_generator.model, jax_generator.params
    tokenizer, mesh = jax_generator.tokenizer, jax_generator.mesh
    tokens = [tokenizer.encode(x, bos=True, eos=False) for x in test_strs]

    # jit model call
    @jax.jit
    def get_logits(params: jnp.ndarray, tokens: jnp.ndarray) -> jnp.ndarray:
        tokens = with_named_sharding_constraint(tokens, mesh, P("dp", None))

        logits = jax_model(
            in_array, 
            params=params, 
        ).logits[:, -1, :]

        logits = with_named_sharding_constraint(logits, mesh, P("dp", None))
        return logits

    # get logits
    jax_logits = []
    for k in range(len(tokens)):
        in_array = jnp.asarray(tokens[k][:jax_model.config.max_sequence_length])[None]
        jax_logits.append(get_logits(jax_params, in_array))
    jax_logits = np.asarray(jnp.concatenate(jax_logits, axis=0))
    # unload jax model
    del jax_model
    del jax_params
    del get_logits
    
    
    # get pytorch logits
    torch_generator = torch_load.build(
        ckpt_dir, 
        tokenizer_path,
        max_seq_len=8192, 
        max_batch_size=1, 
        model_parallel_size=1,
        seed=1,
    )
    torch_model, tokenizer = torch_generator.model, torch_generator.tokenizer
    tokens = [tokenizer.encode(x, bos=True, eos=False) for x in test_strs]
    torch_logits = []
    for k in range(len(tokens)):
        in_array = torch.tensor(tokens[k][:torch_model.params.max_seq_len]).long().cuda().unsqueeze(0)
        torch_logits.append(torch_model.forward(in_array, 0)[:, -1, :])
    torch_logits = torch.cat(torch_logits, dim=0).detach().cpu().numpy()
    # unload pytorch model
    del torch_generator
    del torch_model

    assert np.allclose(jax_logits, torch_logits, atol=atol), "ModelLogits test failed"
    
    return np.max(np.abs(jax_logits - torch_logits).reshape(len(test_strs), -1), axis=1)


def test_ModelGenerations(ckpt_dir: str, tokenizer_path: str, test_strs: List[str], gen_len: int=32) -> None:
    assert torch.cuda.is_available(), "CUDA is not available."
    assert jax.lib.xla_bridge.get_backend().platform == "gpu"
    
    # load jax model
    jax_generator = jax_load(
        ckpt_dir,
        tokenizer_path,
        is_llama3=True,
        max_seq_length=8192,
        # precision='highest',
    )
    jax_strs = jax_generator.generate_from_str(test_strs, max_gen_len=gen_len, temperature=0.0, top_p=1.0)
    # unload jax model
    del jax_generator

    # get pytorch strs
    torch_generator = torch_load.build(
        ckpt_dir, 
        tokenizer_path,
        max_seq_len=8192, 
        max_batch_size=len(test_strs), 
        model_parallel_size=1,
        seed=1,
    )
    torch_strs = torch_generator.text_completion(test_strs, max_gen_len=gen_len, temperature=0.0, top_p=1.0)
    torch_strs = list(map(lambda x: x['generation'], torch_strs))
    # unload pytorch model
    del torch_generator.model
    del torch_generator

    assert all([jax_strs[i].removeprefix('<|begin_of_text|>').strip().removeprefix(test_strs[i]).strip() == torch_strs[i].strip() for i in range(len(test_strs))]), "ModelGenerations test failed"

def main(ckpt_dir: str = "/root/tt/models/8B", tokenizer_path: str = "/root/tt/models/tokenizer.model"):
    np.random.seed(0)

    with torch.no_grad():
        with jax.default_device(jax.devices('cpu')[0]):
            print('='*10)
            print("[Testing RMSNorm]")
            errs = test_RMSNorm(ModelArgs(), 1, atol=1e-2)
            print("[Passed]")
            print("Max RMSNorm error: %f" % (np.max(errs)))
            print("Mean RMSNorm error: %f" % (np.mean(errs)))
            print("Median RMSNorm error: %f" % (np.median(errs)))
            print('='*10)
            print('='*10)
            print("[Testing precompute_freqs_cis]")
            errs = test_precompute_freqs_cis(ModelArgs(), atol=1e-2)
            print("[Passed]")
            print("Max precompute_freqs_cis error: %f" % (np.max(errs)))
            print("Mean precompute_freqs_cis error: %f" % (np.mean(errs)))
            print("Median precompute_freqs_cis error: %f" % (np.median(errs)))
            print('='*10)

            print('='*10)
            print("[Testing apply_rotary_emb]")
            errs0, errs1, _1, _2 = test_apply_roary_emb(ModelArgs(), 1, atol=1e-2)
            print("[Passed]")
            print("Max apply_rotary_emb error: %f, %f" % (np.max(errs0), np.max(errs1)))
            print("Mean apply_rotary_emb error: %f, %f" % (np.mean(errs0), np.mean(errs1)))
            print("Median apply_rotary_emb error: %f, %f" % (np.median(errs0), np.median(errs1)))
            print('='*10)

            print('='*10)
            print("[Testing Attention]")
            errs, _ = test_Attention(ModelArgs(), 1, atol=1e-2)
            print("[Passed]")
            print("Max Attention error: %f" % (np.max(errs)))
            print("Mean Attention error: %f" % (np.mean(errs)))
            print("Median Attention error: %f" % (np.median(errs)))
            print('='*10)

            print('='*10)
            print("[Testing FeedForward]")
            errs,_ = test_feedForward(ModelArgs(), 1, atol=1e-2)
            print("[Passed]")
            print("Max FeedForward error: %f" % (np.max(errs)))
            print("Mean FeedForward error: %f" % (np.mean(errs)))
            print("Median FeedForward error: %f" % (np.median(errs)))
            print('='*10)

            print('='*10)
            print("[Testing TransformerBlock]")
            errs, _ = test_TransformerBlock(ModelArgs(), 1, atol=1e-2)
            print("[Passed]")
            print("Max TransformerBlock error: %f" % (np.max(errs)))
            print("Mean TransformerBlock error: %f" % (np.mean(errs)))
            print("Median TransformerBlock error: %f" % (np.median(errs)))
            print('='*10)

            print('='*10)
            print("[Testing Transformer]")
            errs = test_Transformer(ModelArgs(), 128, atol=1e-2)
            print("[Passed]")
            print("Max Transformer error: %f" % (np.max(errs)))
            print("Mean Transformer error: %f" % (np.mean(errs)))
            print("Median Transformer error: %f" % (np.median(errs)))
            print('='*10)
                
        """

        print('='*10)
        print("[Testing Tokenizer]")
        test_Tokenizer(
            tokenizer_path,
            [
                "The capital of Germany is the city of", 
                "Here is my sonnet in the style of Shakespeare about an artificial intelligence:", 
            ], 
        )
        print("[Passed]")
        print('='*10)
        

        print('='*10)
        print("[Testing ModelLogits]")
        errs = test_ModelLogits(
            ckpt_dir, 
            tokenizer_path,
            local_rank, 
            world_size, 
            [
                "The capital of Germany is the city of", 
                "Here is my sonnet in the style of Shakespeare about an artificial intelligence:", 
            ], 
            atol=5e-1, 
        )
        print("[Passed]")
        print("Max ModelLogits error: %f" % (np.max(errs)))
        print("Mean ModelLogits error: %f" % (np.mean(errs)))
        print("Median ModelLogits error: %f" % (np.median(errs)))
        print('='*10)

        print('='*10)
        print("[Testing ModelGenerations]")
        test_ModelGenerations(
            ckpt_dir, 
            tokenizer_path,
            local_rank, 
            world_size, 
            [
                "The capital of Germany is the city of", 
                "The translation of \"hello world\" to Spanish is", 
            ], 
            gen_len=32, 
        )
        print("[Passed]")
        print('='*10)
        """

if __name__ == "__main__":
    fire.Fire(main)