"""Run the real MLX backend twice with temporary tiny Qwen3 weights; no model download."""

import asyncio
import json
from pathlib import Path
import sys
import tempfile

import mlx.core as mx
from mlx.utils import tree_flatten
from mlx_lm.models.qwen3 import Model, ModelArgs
from tokenizers import Tokenizer
from tokenizers.models import WordLevel
from tokenizers.pre_tokenizers import Whitespace
from transformers import PreTrainedTokenizerFast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ai_setup.bundle import load_backend

def main():
    with tempfile.TemporaryDirectory(prefix='pythona_mlx_smoke_') as directory:
        path = Path(directory)
        vocab = {word: index for index, word in enumerate(['<unk>', '<eos>', '<tool_call>', '</tool_call>', '<think>', '</think>', 'system', 'user', 'assistant', 'hello', 'world', 'answer', 'read_file', 'path', 'a.py'])}
        tokenizer = Tokenizer(WordLevel(vocab=vocab, unk_token='<unk>'))
        tokenizer.pre_tokenizer = Whitespace()
        wrapped = PreTrainedTokenizerFast(tokenizer_object=tokenizer, unk_token='<unk>', eos_token='<eos>')
        wrapped.chat_template = "{% for message in messages %}{{ message['role'] }} {{ message['content'] }} {% endfor %}{% if add_generation_prompt %}assistant {% endif %}"
        wrapped.save_pretrained(path)
        args = dict(model_type='qwen3', hidden_size=32, num_hidden_layers=1, intermediate_size=64,
                    num_attention_heads=2, num_key_value_heads=1, head_dim=16, rms_norm_eps=1e-6,
                    vocab_size=len(vocab), max_position_embeddings=512, rope_theta=10000.0,
                    tie_word_embeddings=True)
        model = Model(ModelArgs(**args))
        mx.eval(model.parameters())
        mx.save_safetensors(str(path / 'model.safetensors'), dict(tree_flatten(model.parameters())))
        (path / 'config.json').write_text(json.dumps(args))
        del model
        mx.clear_cache()
        namespace = load_backend()
        request = {'backend': 'mlx_lm', 'model_id': str(path), 'instructions': 'Answer briefly',
                   'messages': [{'role': 'user', 'content': 'hello'}], 'tools': [], 'maximum_response_tokens': 4}
        async def invoke(name, arguments):
            raise AssertionError('No tools expected')
        async def main():
            results = []
            for _ in range(2):
                events = [event async for event in namespace['mlx_model_events'](request, invoke)]
                assert events[-1]['type'] == 'finish', events
                assert any(event.get('delta') for event in events), events
                results.append(events[-1])
            assert results[-1]['memory']['cache'] == 0, results
            assert results[-1]['memory']['active'] <= results[0]['memory']['active'] + 4096, results
            print(json.dumps({'real_mlx_tiny_qwen3': results}, indent=2))
        asyncio.run(main())


if __name__ == "__main__":
    main()
