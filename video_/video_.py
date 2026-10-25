"""
Видео как материал: кадры по расписанию и лица из них.

Видео смотрят не целиком: нужна минута, на которой человек повернулся, или
ответ на вопрос «есть ли тут вообще лицо». `video_frames` нарезает ffmpeg'ом
полосу кадров с таймкодами (кадр N — секунда `(N-1)·every`), `video_faces`
проходит нарезку детектором и выкладывает лучшие кропы: «кто в кадре и в
каком качестве» видно одной картинкой, а не перебором.

Детектор — YuNet (ONNX внутри OpenCV, модель ~230 КБ качается один раз в
`~/.dsh/models`): на мелком лице 370p-исходника, где Haar-каскады дают один
два срабатывания, ловит устойчиво; insightface (`ai_face`) для этого не нужен
— там сверка с якорем, здесь только «найти и вырезать».

Качество кропа — площадь лица × резкость (дисперсия Лапласа на сером):
маленькое, но мыльное лицо проигрывает среднему в фокусе. Кроп — квадрат
вокруг бокса лица с отступом на лоб и подбородок, приведённый к 256 px;
исходник 640×360 ⇒ крупные кропы — апскейл, это нормально для разглядывания,
но для поиска по лицу годится ровно то, что исходник.

Родственное: `ai_face` (сверка эмбедингов, чьё это лицо), `image_sheet`
(сетка файлов), `adb_rec` (запись экрана телефона, там же ffmpeg-разбор).
"""

import argparse
import json
import shutil
import subprocess
import urllib.request
from pathlib import Path

VIDEO_FRAMES_DIR = ''  # куда по умолчанию кладутся кадры; '' — рядом с видео
VIDEO_FACES_SIZE = 256  # сторона кропа; больший апскейл мелкого лица — самообман
VIDEO_FACES_SCORE = 0.6  # порог детектора YuNet; ниже — пополам с мусором
VIDEO_FACES_MARGIN = 0.55  # отступ бокса как доля стороны лица (лоб+шея)

YUNET_URL = ('https://github.com/opencv/opencv_zoo/raw/main/models/'
             'face_detection_yunet/face_detection_yunet_2023mar.onnx')
YUNET_PATH = Path.home() / '.dsh' / 'models' / 'yunet2023.onnx'


def video_duration(video) -> float:
    """Секунды видео через ffprobe; 0.0 — ffmpeg не видит файл вообще."""
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(video)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip())
    except ValueError:
        return 0.0


def video_frames(video, every_sec: float = 2.0, out: str = '', width: int = 0) -> list:
    """Кадры каждые `every_sec` секунд: `[{path, sec}]`, отсортированы по времени.

    `out` — каталог; пусто — `<видео>.frames/` рядом с файлом. `width` —
    привести ширину (высота пропорционально): для разглядывания не надо, для
    детектора и листов — меньше мусора на диске. Кадр N лежит на секунде
    `(N-1)·every_sec` — это и есть `sec`.
    """
    video = Path(video)
    if shutil.which('ffmpeg') is None:
        return []

    out_dir = Path(out or video.with_suffix(video.suffix + '.frames'))
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob('f_*.jpg'):
        stale.unlink()

    vf = f'fps={1 / every_sec}' + (f',scale={width}:-1' if width else '')
    r = subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-i', str(video), '-vf', vf, str(out_dir / 'f_%04d.jpg')],
                       capture_output=True, text=True)
    if r.returncode:
        return []

    return [{'path': str(p), 'sec': i * every_sec}
            for i, p in enumerate(sorted(out_dir.glob('f_*.jpg')))]


def video_faces(video, out: str = '', every_sec: float = 2.0, top: int = 12,
                min_side: int = 40) -> list:
    """Лучшие лица видео: `[{path, sec, score, side, sheet}]` по убыванию качества.

    Кропы — `<out>/crop_NNN.jpg` (пронумерованы по качеству), лист —
    `<out>/faces_sheet.jpg`; `sheet` у первой записи. `min_side` отсекает
    лица-пиксели (меньше — опознать нельзя, только размыть статистику). Дубли
    одного и того же поворота (IoU > 0.6 между соседними кадрами) не плодит
    десятки почти одинаковых кропов: на каждый поворот — один лучший.
    """
    import cv2  # локально: без opencv модуль импортируется, но faces скажет словами

    out_dir = Path(out or str(Path(video).with_suffix(video.suffix + '.faces')))
    out_dir.mkdir(parents=True, exist_ok=True)
    found = []
    for fr in video_frames(video, every_sec, str(out_dir / 'frames'), width=640):
        img = cv2.imread(fr['path'])
        for face in _yunet_faces(cv2, img):
            side = int(max(face[2:4]) * (1 + VIDEO_FACES_MARGIN))
            crop, sharp = _face_crop(cv2, img, face, side)
            if crop is None or max(face[2:4]) < min_side:
                continue
            found.append({'sec': fr['sec'], 'box': (int(face[0]), int(face[1]), side),
                          'score': max(face[2:4]) * sharp, 'img': img, 'crop': crop})

    found = _drop_same_turns(found)
    found.sort(key=lambda f: -f['score'])

    from image_.image_sheet import image_sheet

    crops, paths = [], []
    for i, f in enumerate(found[:top]):
        p = out_dir / f'crop_{i:03d}.jpg'
        cv2.imwrite(str(p), cv2.resize(f['crop'], (VIDEO_FACES_SIZE, VIDEO_FACES_SIZE),
                                       interpolation=cv2.INTER_CUBIC))
        crops.append((f['sec'], f['score'], f['box']))
        paths.append(str(p))

    sheet = ''
    if paths:
        sheet = image_sheet(paths, str(out_dir / 'faces_sheet.jpg'), cols=6)['out']

    return [{'path': p, 'sec': c[0], 'score': round(c[1]), 'box': c[2],
             'sheet': sheet if i == 0 else ''} for i, (p, c) in enumerate(zip(paths, crops))]


def video_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='видео → кадры и лица')
    sub = parser.add_subparsers(dest='command', required=True)

    dur = sub.add_parser('duration', help='секунды видео')
    dur.add_argument('video')

    frames = sub.add_parser('frames', help='кадры по расписанию')
    frames.add_argument('video')
    frames.add_argument('--every', type=float, default=2.0, help='секунд между кадрами')
    frames.add_argument('--out', default='', help='каталог кадров')
    frames.add_argument('--width', type=int, default=0, help='привести ширину')

    faces = sub.add_parser('faces', help='лучшие лица + лист')
    faces.add_argument('video')
    faces.add_argument('--every', type=float, default=2.0)
    faces.add_argument('--out', default='')
    faces.add_argument('--top', type=int, default=12)
    faces.add_argument('--min', type=int, default=40, help='минимальная сторона лица')

    args = parser.parse_args(argv)
    if args.command == 'duration':
        print(video_duration(args.video))
    elif args.command == 'frames':
        rows = video_frames(args.video, args.every, args.out, args.width)
        print('\n'.join(f"{r['sec']:7.1f} {r['path']}" for r in rows) or 'кадров нет')
    else:
        rows = video_faces(args.video, args.out, args.every, args.top, args.min)
        if not rows:
            print('лиц не найдено')
        for r in rows:
            print(f"{r['sec']:7.1f}с  score {r['score']:6d}  {r['path']}")
        if rows:
            print(f"лист: {rows[0]['sheet']}")

    return 0



def _yunet_faces(cv2, img):
    """Все лица кадра: строки `x, y, w, h, conf` YuNet'а."""
    if not YUNET_PATH.is_file():
        YUNET_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(YUNET_URL, YUNET_PATH)

    det = cv2.FaceDetectorYN_create(str(YUNET_PATH), '', (0, 0), VIDEO_FACES_SCORE)
    det.setInputSize((img.shape[1], img.shape[0]))
    _, rows = det.detect(img)

    return rows if rows is not None else []


def _face_crop(cv2, img, face, side):
    """Квадрат вокруг лица с отступом; за край кадра — то, что есть, и None если пусто."""
    cx = int(face[0] + face[2] / 2)
    cy = int(face[1] + face[3] / 2 - side * 0.1)  # лицо в кадре чуть ниже центра
    h, w = img.shape[:2]
    x0, y0 = max(cx - side // 2, 0), max(cy - side // 2, 0)
    crop = img[y0:min(cy + side // 2, h), x0:min(cx + side // 2, w)]
    if crop.size == 0 or min(crop.shape[:2]) < 16:
        return None, 0.0

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    return crop, float(cv2.Laplacian(gray, cv2.CV_32F).var())


def _drop_same_turns(found, iou=0.6):
    """Соседние кадры с тем же поворотом — оставляем лучший по score."""
    kept = []
    for f in found:
        dup = next((k for k in kept if abs(k['sec'] - f['sec']) <= 2.5
                    and _iou(k['box'], f['box']) > iou), None)
        if dup and dup['score'] < f['score']:
            kept[kept.index(dup)] = f
        elif not dup:
            kept.append(f)

    return kept


def _iou(a, b) -> float:
    ax, ay, aw = a
    bx, by, bw = b
    ix = max(min(ax + aw, bx + bw) - max(ax, bx), 0)
    iy = max(min(ay + aw, by + bw) - max(ay, by), 0)
    inter = ix * iy

    return inter / (aw * aw + bw * bw - inter or 1)


if __name__ == '__main__':
    raise SystemExit(video_main())
