import torch
from diffusers import AutoPipelineForText2Image
from PIL import Image, ImageDraw, ImageFont

def add_text_to_image(img, text):
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf", 16)
    except:
        font = ImageFont.load_default()

    # Рисуем черную подложку внизу
    width, height = img.size
    draw.rectangle([0, height - 40, width, height], fill="black")
    # Пишем текст
    draw.text((10, height - 30), text, font=font, fill="white")
    return img

# Загружаем турбо-модель (она легче стандартных)
pipe = AutoPipelineForText2Image.from_pretrained(
    "stabilityai/sdxl-turbo",
    torch_dtype=torch.float32  # Для CPU используем float32
)

# Настройка для работы на процессоре
pipe.to("cpu")

persons = {
    'robot R-600': 'combat robot, matte gray titanium body, glowing blue visor',
    'bandits': 'in red masks and leather jackets',
}

prompt_default = "B&W comic book style, ink drawing, 320x448"

# Промпт для черно-белого комикса
data = [
    {
        "img_desc": "A heavy robot R-600 sitting in a dark corner of the 'Oil Tank' diner, a single shaft of light illuminating a tiny espresso cup in his massive iron hand, steam rising.",
        "text": "В этом масле явно не хватало присадок, но это был лучший эспрессо в этом секторе."
    },
    {
        "img_desc": "Close-up of robot R-600's optical sensor flickering from blue to deep violet as he 'drinks', internal gears whirring behind a semi-transparent neck plate.",
        "text": "Сканирование вкуса... Заметки: терпко, с привкусом ржавчины. Мне нравится."
    },
    {
        "img_desc": "Atmospheric wide shot: a lonely waitress cleaning a neon-lit bar, shadows of ceiling fans spinning, a jukebox in the corner playing silent music.",
        "text": "Тишина здесь была такой густой, что её можно было резать лазером."
    },
    {
        "img_desc": "The front window shatters as three bandits with heavy cybernetic implants and shotguns kick the door open, glass raining down like diamonds.",
        "text": "Бам! Громкая музыка сменилась симфонией битого стекла."
    },
    {
        "img_desc": "A bandit with a red mask slams his boot on a table, pointing a sawed-off shotgun at the waitress's head, terror reflected in her eyes.",
        "text": "— Кассу на стол, крошка! Иначе я превращу твой мозг в цифровой мусор!"
    },
    {
        "img_desc": "Robot R-600 slowly placing his cup on the saucer, his shoulder servos locking into combat mode with a heavy metallic 'click'.",
        "text": "Директива 4: Защита гражданских. Режим вежливости: Отключен."
    },
    {
        "img_desc": "Action freeze-frame: robot R-600 draws a glowing blue energy cannon from his thigh, the first shot ionizing the air with a blinding streak of light.",
        "text": "— У вас 0.04 секунды, чтобы бросить оружие. Время вышло."
    },
    {
        "img_desc": "One bandit is blasted backward through a wooden partition, blue sparks dancing on his armor, debris flying in slow motion.",
        "text": "Первый пошел. Система оценивает точность: 99.8%."
    },
    {
        "img_desc": "The cafe turns into a war zone, bullets leaving trails in the air, robot R-600 walking through a hailstorm of lead, unmoved and lethal.",
        "text": "Свинец — это плохой аргумент против титанового сплава."
    },
    {
        "img_desc": "The surviving bandits scramble toward a rusted armored van outside, firing blindly over their shoulders in pure panic.",
        "text": "— Валим! Это не робот, это ходячий утилизатор!"
    },
    {
        "img_desc": "Robot R-600 standing in the doorway, the desert sun behind him creating a silhouette, his sensors glowing red as he locks onto the target.",
        "text": "Попытка бегства зафиксирована. Начисляю штраф за превышение скорости."
    },
    {
        "img_desc": "Low-angle shot: robot R-600 looks at his shattered espresso cup and a stain on his metallic chest, his fist clenching with a screech of metal.",
        "text": "Это была моя любимая чашка. И мой последний чистый воротник."
    },
    {
        "img_desc": "Robot R-600 marching onto the cracked asphalt, a heavy dust storm beginning to brew, his revolver humming with power.",
        "text": "Пустыня скроет всё. Кроме моего гнева."
    },
    {
        "img_desc": "Robot R-600 punching a data spike into a parked interceptor bike, the machine's headlights flashing blue as it wakes up with a roar.",
        "text": "Авторизация пройдена. Покатаемся."
    },
    {
        "img_desc": "A chase at 200 mph, robot R-600 leaning the bike so low sparks fly, bandits throwing grenades from the van's roof.",
        "text": "— Эй, парни! Вы уронили кошелек! Сейчас верну!"
    },
    {
        "img_desc": "Robot R-600 jumps the bike over a dune, firing mid-air, the plasma bolt melting the van's axle in a shower of molten metal.",
        "text": "Точка прицеливания: задний мост. Результат: фатальный."
    },
    {
        "img_desc": "The van somersaults through the air, crashing into a giant cactus, a massive fireball erupting in the background.",
        "text": "Приземление: 3 из 10. За артистизм — ноль."
    },
    {
        "img_desc": "Robot R-600 stands over the crawling bandits, the barrel of his gun smoking, his red visor reflecting the flames of the wreck.",
        "text": "— Итак, господа. Кофе стоил пять кредитов. С вас — пять тысяч. За моральный ущерб и разбитую посуду."
    }
]

for k, item in enumerate(data):
    # Создаем копию описания для модификации
    enhanced_desc = item['img_desc']

    # Заменяем упоминания персонажей их подробным описанием
    for name, description in persons.items():
        name = name.lower()
        enhanced_desc = enhanced_desc.lower()
        if name in enhanced_desc:
            enhanced_desc = enhanced_desc.replace(name, f"{name} ({description})")

    # Формируем итоговый промпт
    full_prompt = f"{prompt_default}, {enhanced_desc}"

    # Генерация
    image = pipe(
        prompt=full_prompt,
        num_inference_steps=1,
        guidance_scale=0.0,
        width=320,
        height=448
    ).images[0]

    # Накладываем текст и сохраняем
    image_with_text = add_text_to_image(image, item['text'])
    image_with_text.save(f"story_{k}.png")
    print(f"Кадр {k} готов (с заменой персон): {item['text']}")