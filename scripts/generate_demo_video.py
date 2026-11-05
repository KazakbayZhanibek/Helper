"""Create a deterministic, silent 60-second UI test source. No user media is read.

This is a diagram of a sample packing process, not a real workplace recording.
It exists to exercise seeking, timing, publication, and protected playback.
"""
from pathlib import Path
import subprocess
import tempfile
import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'backend/private-media/demo-process.mp4'


def font(size):
    for candidate in (
        '/System/Library/Fonts/Supplemental/Arial.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
    ):
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    return ImageFont.load_default(size=size)


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    steps = [
        ('01', 'Проверьте состав набора', 'Три учебных предмета — один комплект'),
        ('02', 'Уложите предметы в коробку', 'Каждый предмет занимает своё место'),
        ('03', 'Добавьте карточку комплекта', 'Сверьте содержимое со списком'),
        ('04', 'Закройте и отметьте комплект', 'Набор готов к передаче'),
    ]
    with tempfile.TemporaryDirectory() as temp:
        for index, (number, title, detail) in enumerate(steps):
            im = Image.new('RGB', (1280, 720), '#E9EBE0')
            d = ImageDraw.Draw(im)
            d.text((64, 45), 'ОПЫТ МАСТЕРА  /  УЧЕБНЫЙ ПРИМЕР', font=font(21), fill='#47604D')
            d.text((64, 100), title, font=font(42), fill='#26382E')
            d.text((64, 160), detail, font=font(23), fill='#536452')
            d.rounded_rectangle((370, 275, 910, 570), radius=18, fill='#C3A57B', outline='#A1825D', width=4)
            d.polygon([(370, 275), (450, 235), (990, 235), (910, 275)], fill='#D8C3A3')
            d.polygon([(910, 275), (990, 235), (990, 525), (910, 570)], fill='#AA8D68')
            if index < 3:
                for item in range(3):
                    x = 415 + item * 145
                    y = 335 if index > 0 else 600
                    d.rounded_rectangle((x, y, x + 105, y + 95), radius=12, fill=['#28624A', '#9CAE79', '#F4F1E7'][item])
                if index >= 2:
                    d.rounded_rectangle((590, 455, 790, 535), radius=6, fill='#F7F6F2')
                    d.text((615, 478), '3 / 3', font=font(26), fill='#26382E')
            else:
                d.rectangle((620, 275, 665, 570), fill='#E0D5AD')
                d.rounded_rectangle((720, 360, 850, 445), radius=7, fill='#F7F6F2')
                d.text((741, 384), 'ГОТОВО', font=font(23), fill='#28624A')
            d.text((64, 280), number, font=font(100), fill='#28624A')
            d.text((64, 665), 'Синтетическая схема · без звука · не запись реального процесса', font=font(19), fill='#536452')
            im.save(Path(temp) / f'step-{index}.png')
        command = [imageio_ffmpeg.get_ffmpeg_exe(), '-y', '-loglevel', 'error']
        for index in range(4):
            command += ['-loop', '1', '-t', '15', '-framerate', '12', '-i', str(Path(temp) / f'step-{index}.png')]
        command += ['-filter_complex', '[0:v][1:v][2:v][3:v]concat=n=4:v=1:a=0,format=yuv420p[v]',
                    '-map', '[v]', '-c:v', 'libx264', '-preset', 'fast', '-crf', '25', '-movflags', '+faststart', str(OUT)]
        subprocess.run(command, check=True, timeout=120)
    print(f'Created {OUT} ({OUT.stat().st_size} bytes; 60000ms)')


if __name__ == '__main__':
    main()
