from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

import torch
import torch.nn as nn
from transformers import GenerationMixin, PreTrainedModel, AutoConfig
from transformers.models.llama.modeling_llama import LlamaModel, LlamaRotaryEmbedding, LlamaConfig

# Extract only the first three layers from Llama3's base model
class SpeechUnitModel(nn.Module):
    def __init__(self, base_model, num_layers=3, output_dim=2050, num_heads=8):
        super(SpeechUnitModel, self).__init__()
        
        # Configuration and base model initialization
        config = LlamaConfig()
        config.num_hidden_layers = num_layers
        # Embedding layers
        self.embed_tokens = base_model.model.embed_tokens
        original_vocab_size, embed_dim = self.embed_tokens.weight.shape

        # (2048 + 2 (EOS + PAD) )codebook * 8 head , 16384 as begin-of-audio, 16385 as end-of-audio
        self.audio_embed = nn.Embedding(16400, embed_dim)
        nn.init.xavier_uniform_(self.audio_embed.weight.data)

        self.token_weights = nn.Parameter(torch.ones(8))

        # Transformer layers
        self.layers = torch.nn.ModuleList(base_model.model.layers[:num_layers])
        for i in range(len(self.layers)):
            self.layers[i].self_attn.is_causal = True

        self.norm = base_model.model.norm
        self.rotary_emb = LlamaRotaryEmbedding(config=config)

        # Prediction heads
        self.heads = nn.ModuleList([nn.Linear(embed_dim, output_dim) for _ in range(num_heads)])

    def forward(self, input_ids, audio_ids=None, attention_mask=None, position_ids=None, position_embeddings=None):
        '''
        Be aware that the length of input_ids should be the same as audio_ids.
        '''
        if position_ids is None:
            batch_size, seq_len = input_ids.shape
            position_ids = torch.arange(0, seq_len, dtype=torch.long, device=input_ids.device)
            position_ids = position_ids.unsqueeze(0).expand(batch_size, seq_len)

        # hidden_states shape: (batch_size, seq_len, hidden_dim)
        hidden_states = self.embed_tokens(input_ids)

        if audio_ids is not None:
            # audio_ids shape: (8, seq_len)
            audio_embedding = self.audio_embed(audio_ids)   # shape: (8, seq_len, embed_dim)
            weight_audio = torch.sum(audio_embedding * self.token_weights.view(1, -1, 1, 1), dim=1)

            hidden_states = hidden_states + weight_audio

        if position_embeddings is None:
            position_embeddings = self.rotary_emb(hidden_states, position_ids)

        extended_attention_mask = None
        if attention_mask is not None:
            extended_attention_mask = self._extend_attention_mask(attention_mask, hidden_states.device)

        for layer in self.layers:
            hidden_states = layer(
                hidden_states, attention_mask=extended_attention_mask, position_embeddings=position_embeddings
            )[0]

        hidden_states = self.norm(hidden_states)

        # Prediction heads
        logits = [head(hidden_states) for head in self.heads]
        logits = torch.stack(logits, dim=1)  # Output dimension: (batch size, 8, output_dim)
        return logits

    def _extend_attention_mask(self, attention_mask, device):
        return (1.0 - attention_mask[:, None, None, :]) * -10000.0



if __name__ == "__main__":
    model_name = "meta-llama/Meta-Llama-3-8B"

    # Initialize model with extracted weights, and set output dim as mimi codebook size (2048)
    speech_model = SpeechUnitModel()  # Example output_dim

    prompt = "hello world!"
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    input_token, attention_mask = tokenizer(prompt, return_tensors='pt').values()
    from datasets import load_dataset
    ds = load_dataset("anthony-wss/Soundon-tts", streaming="True")
    for data in ds['train']:
        test_text = data['text']
        test_case = torch.tensor(data['unit'])
        print(torch.tensor(data['unit']).shape)
        break
    test_unit = test_case[:, :4]
    test_unit = test_unit.unsqueeze(0)
    base_model = AutoModelForCausalLM.from_pretrained( 
                model_name,  
                device_map="cpu",
                torch_dtype="float",
                trust_remote_code=True,  
                # attn_implementation="flash_attention_2"
            )
    first_three_layers = {}
    for key, value in base_model.model.state_dict().items():
        if key.startswith("embed") or key.startswith("layers.0.") or key.startswith("layers.1.") or key.startswith("layers.2.") or key.startswith("layers.3.") or key.startswith("layers.4.") or key.startswith("layers.5.") or key.startswith("norm"):
            first_three_layers[key] = value
    speech_model.ref_model.load_state_dict(first_three_layers, strict=False)  # 'strict=False' allows partial loading
    speech_model = speech_model.to('cuda')
    input_token, attention_mask = tokenizer(prompt, return_tensors='pt').values()
    outputs = speech_model(input_token.to('cuda'), audio_ids=test_unit.to('cuda'), attention_mask=attention_mask.to('cuda'))
    predicted_tokens = torch.argmax(outputs, dim=-1)
    print(predicted_tokens)