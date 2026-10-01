"""
Сессия X11 машины: сыскать DISPLAY/XAUTHORITY и снарядить окружение headed-браузера.

Headless-хром «as-managed» отбивает капчу (navigator.webdriver=true — тупой
отказ без ошибки), headless-Firefox из Playwright-build тоже: нужен обычный
headed-браузер, а headed требует X-сервер. Без DISPLAY/XAUTHORITY запуск
`launchPersistentContext` без headless висит «Timed out while looking for a
matching extension», и агент начинает чинить профиль, которого не трогал.

Сессию берут от пользователя (DISPLAY=:1, XAUTHORITY=/run/user/<uid>/gdm/
Xauthority), а не из env демона, где DISPLAY пуст: отсюда «DISPLAY пуст —
значит сессии X11 нет» вместо ложного «headed не годен».
"""
import os
import socket

# ── константы ──

X11_DISPLAY_FALLBACKS = (':1', ':0')  # обход, если DISPLAY пуст: gdm-сессия сидит на :1
X11_XAUTH_CANDIDATES = ('/run/user/{uid}/gdm/Xauthority', '/run/gdm/Xauthority',
                        '/run/lightdm/Xauthority', '/home/{uid}/.Xauthority')
X11_TMP_PROFILE_ROOT = '/tmp/.dsh-x11-profile'  # временные профили headed-браузеров

# ── публичный API модуля ──


def x11_display_find(env: dict = None) -> tuple:
    """Сыскать DISPLAY: из env, а где пусто — с обходных X-сокетов по N.

    Args:
        env: окружение процесса (os.environ по умочалению).

    Returns:
        (display, None) либо (None, 'текст ошибки').
    """
    environ = dict(os.environ) if env is None else env
    display = (environ.get('DISPLAY') or '').strip()
    if display and x11_display_is_open(display)[0]:
        return display, None
    for guess in X11_DISPLAY_FALLBACKS:
        if x11_display_is_open(guess)[0]:
            return guess, None
    return None, ('сессии X11 нет: DISPLAY пуст, и на ' + ', '.join(X11_DISPLAY_FALLBACKS) +
                  ' нет слушателя :6000+N — headed-браузер не поднять, работай headless')


def x11_display_is_open(display: str) -> tuple:
    """Открыт ли X-сервер: TCP-коннект на :6000+N (X11-over-TCP, как в Xlib).

    Args:
        display: DISPLAY вида ':1' либо 'host:1'.

    Returns:
        (bool, None) либо (None, 'текст ошибки').
    """
    num, err = x11_display_num(display)
    if err:
        return None, err
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1.0)
    try:
        return sock.connect_ex(('127.0.0.1', 6000 + num)) == 0, None
    except OSError as err:
        return None, f'X-сокета :{6000 + num} не достать: {err}'
    finally:
        sock.close()


def x11_xauthority_find(display: str = '', env: dict = None) -> tuple:
    """Сыскать файл Xauthority: env демона, потом candidates по uid (env — кривой).

    Args:
        display: DISPLAY вида ':1' (пока не трогается; имя сервера в файле не ищется);
            пусто — любой.
        env: окружение процесса (os.environ по умочалению).

    Returns:
        (путь, None) либо (None, 'текст ошибки').
    """
    environ = dict(os.environ) if env is None else env
    if environ.get('XAUTHORITY') and os.path.isfile(environ['XAUTHORITY']):
        return environ['XAUTHORITY'], None
    uid = os.getuid()
    for cand in X11_XAUTH_CANDIDATES:
        path = cand.format(uid=uid, user=os.environ.get('USER', ''))
        if os.path.isfile(path):
            return path, None
    return None, ('файла Xauthority не сыскан: перебирай ' +
                  ', '.join(c.format(uid=os.getuid(), user=os.environ.get('USER', ''))
                            for c in X11_XAUTH_CANDIDATES))


def x11_display_env(profile_dir: str = '', display: str = '', xauthority: str = '',
                    env: dict = None, tmp_profile_root: str = X11_TMP_PROFILE_ROOT) -> tuple:
    """Снарядить env headed-запуска: DISPLAY+XAUTHORITY в os.environ и профиль в tmp.

    Возвращает env-мап для subprocess.Popen(env=...) и путь профиля; тот же
    профиль кладут в `launchPersistentContext(profile_dir, headless=False)`.

    Args:
        profile_dir: каталог persistent-профиля; пусто — создать временный.
        display: DISPLAY; пусто — сыскать.
        xauthority: путь Xauthority; пусто — сыскать.
        env: окружение-основа (os.environ по умочалению).
        tmp_profile_root: корень временных профилей.

    Returns:
        ({'env': dict, 'display': str, 'xauthority': str, 'profile_dir': str}, None)
        либо (None, 'текст ошибки').
    """
    environ = dict(os.environ) if env is None else dict(env)
    if not display:
        display, err = x11_display_find(environ)
        if err:
            return None, err
    opened, err = x11_display_is_open(display)
    if err:
        return None, err
    if not opened:
        return None, f'на DISPLAY {display} нет X-сервера — headed не поднимется'
    if not xauthority:
        xauthority, err = x11_xauthority_find(display, environ)
        if err:
            return None, err
    if not profile_dir:
        profile_dir = os.path.join(tmp_profile_root, 'firefox')
    if not os.path.isdir(profile_dir):
        try:
            os.makedirs(profile_dir, exist_ok=True)
            os.chmod(profile_dir, 0o700)
        except OSError as err:
            return None, f'временный профиль не собрался ({profile_dir}): {err}'
    environ.update({'DISPLAY': display, 'XAUTHORITY': xauthority,
                    'MOZ_HEADLESS': '0', 'DISPLAY_DIR': profile_dir})
    return {'env': environ, 'display': display, 'xauthority': xauthority,
            'profile_dir': profile_dir}, None


def x11_display_num(display: str) -> tuple:
    """Разобрать DISPLAY в номер экрана: ':1' → 1, 'host:1' → 1 (порт :6001).

    Args:
        display: DISPLAY вида ':1' либо 'host:1'.

    Returns:
        (int, None) либо (None, 'текст ошибки').
    """
    for tail in reversed((display or '').split(':')):
        num = tail.split('/')[0]
        if num.isdigit():
            return int(num), None
    return None, f'DISPLAY {display!r} без номера экрана (вид :N)'


def x11_firefox_binary_find(root: str = '') -> tuple:
    """Сыскать бинарь Playwright-Firefox по каталогам сборки (ms-playwright).

    Args:
        root: корень поиска; пусто — PLAYWRIGHT_BROWSERS_PATH, ~/.cache/ms-playwright.

    Returns:
        (путь, None) либо (None, 'текст ошибки').
    """
    roots = [r for r in (os.environ.get('PLAYWRIGHT_BROWSERS_PATH', ''), root,
                         os.path.expanduser('~/.cache/ms-playwright')) if r]
    for walk_root in roots:
        for dir_root, dirs, files in os.walk(walk_root):
            if 'firefox' in files and os.access(os.path.join(dir_root, 'firefox'), os.X_OK):
                return os.path.join(dir_root, 'firefox'), None
    return None, f'бинаря Firefox не сыскан ({", ".join(roots)})'


def x11_launch_args(profile_dir: str, env: dict = None, firefox_binary: str = '',
                     extra_args: tuple = ()) -> tuple:
    """Снарядить argv headed-Firefox под profile-directory: `-no-remote -profile DIR`.

    Args:
        profile_dir: каталог persistent-профиля.
        env: окружение из x11_display_env; пусто — снарядить.
        firefox_binary: путь к бинарю; пусто — сыскать.
        extra_args: хвост argv (-url-флаги и т.п.).

    Returns:
        (argv-list, None) либо (None, 'текст ошибки').
    """
    if not firefox_binary:
        firefox_binary, err = x11_firefox_binary_find()
        if err:
            return None, err
    environ = dict(os.environ) if env is None else dict(env)
    if not environ.get('DISPLAY'):
        display, err = x11_display_find(environ)
        if err:
            return None, err
        environ['DISPLAY'] = display
    argv = [firefox_binary, '-no-remote', '-profile', profile_dir]
    if environ.get('DISPLAY'):
        argv += ['-display', environ['DISPLAY']]
    return argv + list(extra_args) + ['about:blank'], None


def x11_fingerprint_eval() -> str:
    """Вернуть JS-зонд, отличающий автоматизированный браузер от живого.

    'navigator.webdriver=true' в managed Chromium — тупой отказ капчи без
    ошибки; зонд меряет webdriver+chrome+plugins, и по нему видно разницу.

    Returns:
        str — выражение для page.evaluate/browser_run_code_unsafe.
    """
    return ("(() => ({webdriver: !!navigator.webdriver, chrome: !!window.chrome, "
            "plugins: navigator.plugins.length}))()")


if __name__ == '__main__':
    import argparse
    import sys

    _P = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    _P.add_argument('cmd', choices=('env', 'probe'), help='что показать')
    _C = _P.parse_args()
    if _C.cmd == 'env':
        _env, _err = x11_display_env()
        if _err:
            print(f'ошибка: {_err}', file=sys.stderr)
            raise SystemExit(1)
        print(f"DISPLAY={_env['display']} XAUTHORITY={_env['xauthority']} "
              f"профиль={_env['profile_dir']}")
    else:
        print(x11_display_find(), x11_display_is_open(':1'), x11_xauthority_find())
    raise SystemExit(0)
