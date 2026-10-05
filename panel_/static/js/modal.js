// Модальное окно — общее для всех страниц панели.
//
// Одно окно на весь сайт, а не своё на каждой странице: человек ходит между
// страницами, и окно, которое на соседней открывается иначе или закрывается
// другой кнопкой, читается как чужое. Заодно это единственное место, где живут
// скучные, но обязательные мелочи — Escape, возврат прокрутки, возврат фокуса, —
// которые в копиях забывают по одной.
//
// Пользоваться:
//
//     import {modalOpen, modalClose} from './modal.js';
//
//     modalOpen({title: 'Переписка', note: 'Diana ↔ Semen', body: '<div>…</div>'});
//
// `body` — строка разметки или готовый узел. Узлом удобно, когда содержимое живёт
// своей жизнью: обработчики на нём переживут открытие окна.
//
// ⚠ **Строка вставляется как разметка, и экранирует её вызывающий** — тем же
// `esc()`, которым размечается вся панель. Окно не может экранировать за него: ему
// передают готовую разметку, и экранируй оно её, на странице появились бы `&lt;div`
// вместо блоков.
//
// Заголовок и подпись — исключение: они всегда данные (имя собеседника, номер), и
// ставятся текстом (`textContent`). Кавычка в имени не должна рвать шапку окна.
//
// Возвращается корневой узел окна — по нему страница находит своё содержимое,
// чтобы дописать в него что-то потом.

// Открытое окно. Одно на страницу: два окна поверх друг друга — это спор о том,
// какое из них закрывает Escape, и человек в этом споре не участвует.
let modalNode = null;

// Куда вернуть фокус после закрытия. Без этого клавиатура после Escape
// оказывается в начале страницы, а человек — там, откуда открывал окно.
let modalReturn = null;

/**
 * Открывает окно. Уже открытое — закрывает: два окна поверх друг друга не бывают.
 *
 * @param {object} options
 * @param {string} options.title Заголовок. Обязателен: окно без заголовка не
 *   говорит, что в нём, а закрывать его придётся угадывая.
 * @param {string} [options.note] Подпись рядом с заголовком.
 * @param {string|Node} [options.body] Содержимое.
 * @param {boolean} [options.narrow] Узкое окно — для подтверждений.
 * @param {Function} [options.onClose] Позвать при закрытии.
 * @returns {HTMLElement} Корневой узел окна.
 */
export function modalOpen({title, note = '', body = '', narrow = false, onClose = null} = {}) {
    modalClose();

    const back = document.createElement('div');
    back.className = 'modal-back';
    // Роль и подпись — чтобы окно и с клавиатуры читалось как окно, а не как
    // кусок страницы, внезапно оказавшийся посередине.
    back.innerHTML = '<div class="modal' + (narrow ? ' narrow' : '') + '"'
        + ' role="dialog" aria-modal="true">'
        + '<div class="modal-head">'
        + `<span class="modal-title"></span><span class="modal-note"></span>`
        + '<button type="button" class="modal-close" title="Закрыть"'
        + ' aria-label="Закрыть">✕</button>'
        + '</div><div class="modal-body"></div></div>';

    // Заголовок и подпись — текстом, а не разметкой: они приходят из данных
    // (имя собеседника, номер), и кавычка в имени рвала бы разметку.
    back.querySelector('.modal-title').textContent = String(title || '');
    back.querySelector('.modal-note').textContent = String(note || '');

    const bodyBox = back.querySelector('.modal-body');
    if (body instanceof Node) bodyBox.appendChild(body);
    else bodyBox.innerHTML = String(body || '');

    back.querySelector('.modal-close').addEventListener('click', modalClose);

    // Щелчок мимо окна закрывает — но только по самой подложке: клик внутри
    // всплывает до неё, и без сверки цели окно закрывалось бы от выделения текста.
    back.addEventListener('mousedown', (e) => { if (e.target === back) modalClose(); });

    back.__onClose = onClose;
    modalReturn = document.activeElement;
    document.body.appendChild(back);
    // Страница под окном не прокручивается: иначе колесо уводит фон, а человек
    // думает, что двигает содержимое окна.
    document.body.style.overflow = 'hidden';
    document.addEventListener('keydown', modalKey);

    // Фокус на крестик: с клавиатуры окно должно закрываться, ничего не ища.
    back.querySelector('.modal-close').focus();
    modalNode = back;

    return back;
}

/** Закрывает открытое окно. Закрытое — молча ничего не делает. */
export function modalClose() {
    if (!modalNode) return;

    const onClose = modalNode.__onClose;
    document.removeEventListener('keydown', modalKey);
    modalNode.remove();
    modalNode = null;
    document.body.style.overflow = '';

    // Фокус возвращаем туда, откуда открывали, — если оно ещё на странице:
    // список мог перерисоваться, пока окно было открыто.
    if (modalReturn && document.contains(modalReturn)) modalReturn.focus();
    modalReturn = null;

    if (typeof onClose === 'function') onClose();
}

/** Открыто ли окно — страницам, которые не хотят перерисовывать под ним. */
export function modalIsOpen() {
    return !!modalNode;
}

function modalKey(e) {
    if (e.key === 'Escape') modalClose();
}
