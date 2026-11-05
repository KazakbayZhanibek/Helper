"""Isolated, local-only ASR entry point. Never accepts a model download name."""
import argparse
import contextlib
import json
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', required=True, choices=['faster_whisper', 'whisper'])
    parser.add_argument('--model', required=True)
    parser.add_argument('--audio', required=True)
    parser.add_argument('--language', required=True, choices=['ru', 'kk', 'mixed'])
    args = parser.parse_args()
    model_path, audio = Path(args.model), Path(args.audio)
    if not model_path.exists() or not audio.is_file():
        raise ValueError('ASR_INPUT_UNAVAILABLE')
    hint = None if args.language == 'mixed' else args.language
    # Some runtimes print progress to stdout: isolate it from the JSON protocol.
    with contextlib.redirect_stdout(sys.stderr):
        if args.backend == 'faster_whisper':
            if not model_path.is_dir():
                raise ValueError('ASR_MODEL_UNAVAILABLE')
            from faster_whisper import WhisperModel
            model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=2, num_workers=1, local_files_only=True)
            segments, info = model.transcribe(str(audio), language=hint, vad_filter=True, condition_on_previous_text=False, beam_size=5)
            output = [{'start_ms': round(s.start * 1000), 'end_ms': round(s.end * 1000), 'text': s.text.strip()} for s in segments]
            detected = info.language
        else:
            if not model_path.is_file() or model_path.suffix != '.pt':
                raise ValueError('ASR_MODEL_UNAVAILABLE')
            import torch
            import whisper
            torch.set_num_threads(2)
            model = whisper.load_model(str(model_path), device='cpu')
            # Read the normalized PCM WAV ourselves: no secondary ffmpeg lookup.
            import wave
            import numpy as np
            with wave.open(str(audio), 'rb') as stream:
                if stream.getnchannels() != 1 or stream.getframerate() != 16000 or stream.getsampwidth() != 2:
                    raise ValueError('ASR_AUDIO_INVALID')
                samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
            result = model.transcribe(samples, language=hint, fp16=False, condition_on_previous_text=False, verbose=None)
            output = [{'start_ms': round(s['start'] * 1000), 'end_ms': round(s['end'] * 1000), 'text': s['text'].strip()} for s in result['segments']]
            detected = result.get('language')
    # Keep the subprocess protocol and memory bounded before emitting anything.
    if len(output) > 300 or sum(len(s['text']) for s in output) > 18000:
        raise ValueError('TRANSCRIPT_TOO_LARGE')
    print(json.dumps({'segments': output, 'detected_language': detected}, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        # Raw runtime exception strings may contain source paths or text.
        print('ASR_PROCESS_FAILED', file=sys.stderr)
        sys.exit(1)
