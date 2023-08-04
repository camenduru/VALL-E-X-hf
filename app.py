import argparse
import logging
import os
import pathlib
import time
import tempfile
from pathlib import Path
temp = pathlib.WindowsPath
pathlib.WindowsPath = pathlib.PosixPath
os.environ["PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION"] = "python"
import torch
import torchaudio
import random

import numpy as np

from data.tokenizer import (
    AudioTokenizer,
    tokenize_audio,
)
from data.collation import get_text_token_collater
from models.vallex import VALLE
from utils.g2p import PhonemeBpeTokenizer

import gradio as gr
import whisper
torch.set_num_threads(1)
torch.set_num_interop_threads(1)
torch._C._jit_set_profiling_executor(False)
torch._C._jit_set_profiling_mode(False)
torch._C._set_graph_executor_optimize(False)
# torch.manual_seed(42)

lang2token = {
    'zh': "[ZH]",
    'ja': "[JA]",
    "en": "[EN]",
}

lang2code = {
    'zh': 0,
    'ja': 1,
    "en": 2,
}

token2lang = {
    '[ZH]': "zh",
    '[JA]': "ja",
    "[EN]": "en",
}

code2lang = {
    0: 'zh',
    1: 'ja',
    2: "en",
}



langdropdown2token = {
    'English': "[EN]",
    '中文': "[ZH]",
    '日本語': "[JA]",
    'mix': "",
}

text_tokenizer = PhonemeBpeTokenizer(tokenizer_path="./utils/g2p/bpe_69.json")
text_collater = get_text_token_collater()

device = torch.device("cpu")
if torch.cuda.is_available():
    device = torch.device("cuda", 0)

# VALL-E-X model
model = VALLE(
    1024,
    16,
    12,
    norm_first=True,
    add_prenet=False,
    prefix_mode=1,
    share_embedding=True,
    nar_scale_factor=1.0,
    prepend_bos=True,
    num_quantizers=8,
)
checkpoint = torch.load("./epoch-10.pt", map_location='cpu')
missing_keys, unexpected_keys = model.load_state_dict(
    checkpoint["model"], strict=True
)
assert not missing_keys
model.to('cpu')
model.eval()

# Encodec model
audio_tokenizer = AudioTokenizer(device)

# ASR
whisper_model = whisper.load_model("medium").cpu()

def clear_prompts():
    try:
        path = tempfile.gettempdir()
        for eachfile in os.listdir(path):
            filename = os.path.join(path, eachfile)
            if os.path.isfile(filename) and filename.endswith(".npz"):
                lastmodifytime = os.stat(filename).st_mtime
                endfiletime = time.time() - 60
                if endfiletime > lastmodifytime:
                    os.remove(filename)
    except:
        return

def transcribe_one(model, audio_path):
    # load audio and pad/trim it to fit 30 seconds
    audio = whisper.load_audio(audio_path)
    audio = whisper.pad_or_trim(audio)

    # make log-Mel spectrogram and move to the same device as the model
    mel = whisper.log_mel_spectrogram(audio).to(model.device)

    # detect the spoken language
    _, probs = model.detect_language(mel)
    print(f"Detected language: {max(probs, key=probs.get)}")
    lang = max(probs, key=probs.get)
    # decode the audio
    options = whisper.DecodingOptions(beam_size=5)
    result = whisper.decode(model, mel, options)

    # print the recognized text
    print(result.text)

    text_pr = result.text
    if text_pr.strip(" ")[-1] not in "?!.,。，？！。、":
        text_pr += "."
    return lang, text_pr

def make_npz_prompt(name, uploaded_audio, recorded_audio):
    global model, text_collater, text_tokenizer, audio_tokenizer
    clear_prompts()
    audio_prompt = uploaded_audio if uploaded_audio is not None else recorded_audio
    sr, wav_pr = audio_prompt
    wav_pr = torch.FloatTensor(wav_pr) / 32768
    if wav_pr.size(-1) == 2:
        wav_pr = wav_pr.mean(-1, keepdim=False)
    text_pr, lang_pr = make_prompt(name, wav_pr, sr, save=False)

    # tokenize audio
    encoded_frames = tokenize_audio(audio_tokenizer, (wav_pr.unsqueeze(0), sr))
    audio_tokens = encoded_frames[0][0].transpose(2, 1).cpu().numpy()

    # tokenize text
    text_tokens, enroll_x_lens = text_collater(
        [
            text_tokenizer.tokenize(text=f"{text_pr}".strip())
        ]
    )

    message = f"Detected language: {lang_pr}\n Detected text {text_pr}\n"

    # save as npz file
    np.savez(os.path.join(tempfile.gettempdir(), f"{name}.npz"),
             audio_tokens=audio_tokens, text_tokens=text_tokens, lang_code=lang2code[lang_pr])
    return message, os.path.join(tempfile.gettempdir(), f"{name}.npz")


def make_prompt(name, wav, sr, save=True):

    global whisper_model
    whisper_model.to(device)
    if not isinstance(wav, torch.FloatTensor):
        wav = torch.tensor(wav)
    if wav.abs().max() > 1:
        wav /= wav.abs().max()
    if wav.size(-1) == 2:
        wav = wav.mean(-1, keepdim=False)
    if wav.ndim == 1:
        wav = wav.unsqueeze(0)
    assert wav.ndim and wav.size(0) == 1
    torchaudio.save(f"./prompts/{name}.wav", wav, sr)
    lang, text = transcribe_one(whisper_model, f"./prompts/{name}.wav")
    lang_token = lang2token[lang]
    text = lang_token + text + lang_token
    with open(f"./prompts/{name}.txt", 'w') as f:
        f.write(text)
    if not save:
        os.remove(f"./prompts/{name}.wav")
        os.remove(f"./prompts/{name}.txt")

    whisper_model.cpu()
    torch.cuda.empty_cache()
    return text, lang

@torch.no_grad()
def infer_from_audio(text, language, accent, audio_prompt, record_audio_prompt):
    global model, text_collater, text_tokenizer, audio_tokenizer
    audio_prompt = audio_prompt if audio_prompt is not None else record_audio_prompt
    sr, wav_pr = audio_prompt
    wav_pr = torch.FloatTensor(wav_pr)/32768
    if wav_pr.size(-1) == 2:
        wav_pr = wav_pr.mean(-1, keepdim=False)
    text_pr, lang_pr = make_prompt(str(random.randint(0, 10000000)), wav_pr, sr, save=False)
    lang_token = langdropdown2token[language]
    lang = token2lang[lang_token]
    text = lang_token + text + lang_token

    # onload model
    model.to(device)

    # tokenize audio
    encoded_frames = tokenize_audio(audio_tokenizer, (wav_pr.unsqueeze(0), sr))
    audio_prompts = encoded_frames[0][0].transpose(2, 1).to(device)

    # tokenize text
    logging.info(f"synthesize text: {text}")
    text_tokens, text_tokens_lens = text_collater(
        [
            text_tokenizer.tokenize(text=f"{text_pr}{text}".strip())
        ]
    )

    enroll_x_lens = None
    if text_pr:
        _, enroll_x_lens = text_collater(
            [
                text_tokenizer.tokenize(text=f"{text_pr}".strip())
            ]
        )
    lang = lang if accent == "no-accent" else token2lang[langdropdown2token[accent]]
    encoded_frames = model.inference(
        text_tokens.to(device),
        text_tokens_lens.to(device),
        audio_prompts,
        enroll_x_lens=enroll_x_lens,
        top_k=-100,
        temperature=1,
        prompt_language=lang_pr,
        text_language=lang,
    )
    samples = audio_tokenizer.decode(
        [(encoded_frames.transpose(2, 1), None)]
    )

    # offload model
    model.to('cpu')
    torch.cuda.empty_cache()

    message = f"text prompt: {text_pr}\nsythesized text: {text}"
    return message, (24000, samples[0][0].cpu().numpy())

@torch.no_grad()
def infer_from_prompt(text, language, accent, prompt_file):
    # onload model
    model.to(device)
    clear_prompts()
    # text to synthesize
    lang_token = langdropdown2token[language]
    lang = token2lang[lang_token]
    text = lang_token + text + lang_token

    # load prompt
    prompt_data = np.load(prompt_file.name)
    audio_prompts = prompt_data['audio_tokens']
    text_prompts = prompt_data['text_tokens']
    lang_pr = prompt_data['lang_code']
    lang_pr = code2lang[int(lang_pr)]

    # numpy to tensor
    audio_prompts = torch.tensor(audio_prompts).type(torch.int32).to(device)
    text_prompts = torch.tensor(text_prompts).type(torch.int32)

    enroll_x_lens = text_prompts.shape[-1]
    logging.info(f"synthesize text: {text}")
    text_tokens, text_tokens_lens = text_collater(
        [
            text_tokenizer.tokenize(text=f"_{text}".strip())
        ]
    )
    text_tokens = torch.cat([text_prompts, text_tokens], dim=-1)
    text_tokens_lens += enroll_x_lens
    # accent control
    lang = lang if accent == "no-accent" else token2lang[langdropdown2token[accent]]
    encoded_frames = model.inference(
        text_tokens.to(device),
        text_tokens_lens.to(device),
        audio_prompts,
        enroll_x_lens=enroll_x_lens,
        top_k=-100,
        temperature=1,
        prompt_language=lang_pr,
        text_language=lang,
    )
    samples = audio_tokenizer.decode(
        [(encoded_frames.transpose(2, 1), None)]
    )

    # offload model
    model.to('cpu')
    torch.cuda.empty_cache()

    message = f"sythesized text: {text}"
    return message, (24000, samples[0][0].cpu().numpy())


def main():
    app = gr.Blocks()
    with app:
        with gr.Tab("Infer from audio"):
            with gr.Row():
                with gr.Column():

                    textbox = gr.TextArea(label="Text",
                                          placeholder="Type your sentence here",
                                          value="Hello, it's nice to meet you.", elem_id=f"tts-input")
                    language_dropdown = gr.Dropdown(choices=['English', '中文', '日本語', 'mix'], value='English', label='language')
                    accent_dropdown = gr.Dropdown(choices=['no-accent', 'English', '中文', '日本語'], value='no-accent', label='accent')
                    upload_audio_prompt = gr.Audio(label='uploaded audio prompt', source='upload', interactive=True)
                    record_audio_prompt = gr.Audio(label='recorded audio prompt', source='microphone', interactive=True)
                with gr.Column():
                    text_output = gr.Textbox(label="Message")
                    audio_output = gr.Audio(label="Output Audio", elem_id="tts-audio")
                    btn = gr.Button("Generate!")
                    btn.click(infer_from_audio,
                              inputs=[textbox, language_dropdown, accent_dropdown, upload_audio_prompt, record_audio_prompt],
                              outputs=[text_output, audio_output])
                    textbox_mp = gr.TextArea(label="Prompt name",
                                          placeholder="Name your prompt here",
                                          value="prompt_1", elem_id=f"prompt-name")
                    btn_mp = gr.Button("Make prompt!")
                    prompt_output = gr.File(interactive=False)
                    btn_mp.click(make_npz_prompt,
                                inputs=[textbox_mp, upload_audio_prompt, record_audio_prompt],
                                outputs=[text_output, prompt_output])
        with gr.Tab("Make prompt"):
            with gr.Row():
                with gr.Column():
                    textbox2 = gr.TextArea(label="Prompt name",
                                          placeholder="Name your prompt here",
                                          value="prompt_1", elem_id=f"prompt-name")
                    upload_audio_prompt_2 = gr.Audio(label='uploaded audio prompt', source='upload', interactive=True)
                    record_audio_prompt_2 = gr.Audio(label='recorded audio prompt', source='microphone', interactive=True)
                with gr.Column():
                    text_output_2 = gr.Textbox(label="Message")
                    prompt_output_2 = gr.File(interactive=False)
                    btn_2 = gr.Button("Make!")
                    btn_2.click(make_npz_prompt,
                              inputs=[textbox2, upload_audio_prompt_2, record_audio_prompt_2],
                              outputs=[text_output_2, prompt_output_2])
        with gr.Tab("Infer from prompt"):
            with gr.Row():
                with gr.Column():
                    textbox_3 = gr.TextArea(label="Text",
                                          placeholder="Type your sentence here",
                                          value="Hello, it's nice to meet you.", elem_id=f"tts-input")
                    language_dropdown_3 = gr.Dropdown(choices=['English', '中文', '日本語', 'mix'], value='English',
                                                    label='language')
                    accent_dropdown_3 = gr.Dropdown(choices=['no-accent', 'English', '中文', '日本語'], value='no-accent',
                                                  label='accent')
                    prompt_file = gr.File(file_count='single', file_types=['.npz'], interactive=True)
                with gr.Column():
                    text_output_3 = gr.Textbox(label="Message")
                    audio_output_3 = gr.Audio(label="Output Audio", elem_id="tts-audio")
                    btn_3 = gr.Button("Generate!")
                    btn_3.click(infer_from_prompt,
                              inputs=[textbox_3, language_dropdown_3, accent_dropdown_3, prompt_file],
                              outputs=[text_output_3, audio_output_3])

    app.launch()

if __name__ == "__main__":
    formatter = (
        "%(asctime)s %(levelname)s [%(filename)s:%(lineno)d] %(message)s"
    )
    logging.basicConfig(format=formatter, level=logging.INFO)
    main()