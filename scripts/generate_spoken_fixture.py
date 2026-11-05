"""Make a synthetic narrated MP4 for the real upload/ASR smoke check on macOS.

No workplace recording or personal voice is used. Output is an ignored artifact.
"""
from pathlib import Path
import os
import subprocess
import tempfile

from PIL import Image, ImageDraw
from generate_demo_video import font
from dev import load_environment

ROOT = Path(__file__).resolve().parents[1]


def main():
    env = load_environment()
    ffmpeg = env.get('FFMPEG_PATH', 'ffmpeg')
    destination = ROOT / 'artifacts/test-results/packing-spoken.mp4'
    destination.parent.mkdir(parents=True, exist_ok=True)
    narration = ('Проверьте состав учебного набора. Сверьте три предмета с карточкой комплекта. '
                 'Уложите предметы в коробку. Каждый предмет положите на своё место. '
                 'Добавьте карточку комплекта. Проверьте, что все предметы находятся в коробке. '
                 'Закройте коробку. Отметьте завершение комплектации учебного набора.')
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        speech, card = temp / 'speech.aiff', temp / 'card.png'
        subprocess.run(['/usr/bin/say', '-v', 'Milena', '-r', '135', '-o', str(speech), narration], check=True, timeout=60)
        if not speech.exists() or speech.stat().st_size < 10000:
            raise SystemExit('The system speech service produced no audio. Run where macOS speech synthesis is available.')
        image = Image.new('RGB', (1280, 720), '#F4F1E7')
        draw = ImageDraw.Draw(image)
        draw.text((70, 65), 'ОПЫТ МАСТЕРА / ПРОВЕРКА ЗАГРУЗКИ', font=font(25), fill='#28624A')
        draw.text((70, 135), 'Комплектация учебного набора', font=font(45), fill='#26382E')
        for index, title in enumerate(['Проверьте состав набора', 'Уложите предметы в коробку', 'Добавьте карточку комплекта', 'Закройте коробку']):
            draw.text((75, 250 + index * 75), f'0{index + 1}   {title}', font=font(32), fill='#26382E')
        draw.text((70, 650), 'Синтетический тест · голос macOS · не рабочая инструкция', font=font(22), fill='#536452')
        image.save(card)
        subprocess.run([ffmpeg, '-y', '-hide_banner', '-loglevel', 'error', '-loop', '1', '-framerate', '12', '-i', str(card), '-i', str(speech), '-c:v', 'libx264', '-threads', '2', '-preset', 'fast', '-crf', '25', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '96k', '-af', 'apad=pad_dur=1', '-shortest', '-movflags', '+faststart', str(destination)], check=True, timeout=120)
    print(f'Synthetic spoken fixture: {destination} ({destination.stat().st_size} bytes)')


if __name__ == '__main__':
    main()
