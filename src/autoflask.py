# -*- coding: utf-8 -*-
"""
PoE 2 AutoFlask
Автоматическое использование фласок здоровья и маны по цвету пикселя.

Логика:
- Контрольный (золотой) пиксель на рамке проверяет, идёт ли игровой процесс.
  Если открыто меню/инвентарь — экран затемнён, золото тускнеет, фласки не жмутся.
- Пиксель здоровья: если перестал быть красным -> нажать клавишу фласки HP.
- Пиксель маны: если перестал быть синим -> нажать клавишу фласки маны.

Горячие клавиши:
  F9  - пауза / возобновление
  F10 - перекалибровать точки заново
  Ctrl+C (в окне) или закрыть окно - выход
"""

import time
import json
import os
import sys
import random

import pyautogui
import keyboard

try:
    import win32gui
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ==================== НАСТРОЙКИ ПО УМОЛЧАНИЮ ====================
# Эти значения используются только если их нет в конфиг-файле.
# Менять клавиши лучше в самом конфиге (sysmon_settings.json) рядом с программой.
DEFAULT_HP_KEY = "1"    # клавиша фласки здоровья по умолчанию
DEFAULT_MANA_KEY = "2"  # клавиша фласки маны по умолчанию

# Минимальный интервал между срабатываниями фласки, секунд.
# Отдельно для здоровья и маны — работают независимо друг от друга.
# Берутся из конфига, эти значения используются только при их отсутствии.
DEFAULT_HP_COOLDOWN = 3.0
DEFAULT_MANA_COOLDOWN = 3.0

# Технический допуск на «дрожание» цвета пикселя (блики, сглаживание,
# сжатие вывода). Два цвета считаются ОДИНАКОВЫМИ, если расстояние между
# ними в RGB не превышает это значение. Подобрано так, чтобы поглощать
# естественный шум, но уверенно различать полный и опустевший шар
# (разница между ними в разы больше). Менять обычно не требуется.
COLOR_MATCH_TOLERANCE = 35  # единиц евклидова расстояния в RGB (0..441)

CHECK_INTERVAL = 0.1    # пауза между проверками, сек
# Случайная задержка перед нажатием (имитация человеческой реакции), мс
DELAY_MIN_MS = 50
DELAY_MAX_MS = 200
GAME_TITLE = "Path of Exile 2"  # часть заголовка окна игры
GAME_PROCESS = "PathOfExile"    # часть имени .exe процесса игры (запасная проверка)

CONFIG_FILE = "sysmon_settings.json"
LOG_FILE = "sysmon_log.txt"   # сюда пишется история срабатываний
PICK_KEY = "f8"
PAUSE_KEY = "f9"
RECALIBRATE_KEY = "f10"
# ===================================================

paused = False
need_recalibrate = False

pyautogui.FAILSAFE = False  # отключаем аварийный угол, чтобы не мешал


def config_path():
    """Путь к конфигу рядом с .exe / скриптом."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, CONFIG_FILE)


def _pixel_winapi(x, y):
    """Прочитать пиксель напрямую через Windows GDI (без pyscreeze).

    Это основной способ: лёгкий, не тянет лишних библиотек и работает
    в оконном/безрамочном режиме. Возвращает (r, g, b) или бросает ошибку.
    """
    import ctypes
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetDC(0)
    if not hdc:
        raise RuntimeError("GetDC вернул 0")
    try:
        # GetPixel возвращает COLORREF (0x00BBGGRR) или -1 при ошибке
        color = gdi32.GetPixel(hdc, int(x), int(y))
        if color < 0:
            raise RuntimeError("GetPixel вернул -1")
        r = color & 0xFF
        g = (color >> 8) & 0xFF
        b = (color >> 16) & 0xFF
        return (r, g, b)
    finally:
        user32.ReleaseDC(0, hdc)


def read_pixel(x, y):
    """Считать цвет пикселя (R,G,B). При ошибке бросаем понятное исключение.

    Сначала пробуем системный способ через Windows API (надёжно и без
    лишних зависимостей). Если он недоступен — пробуем pyautogui.

    Если оба способа не сработали, самая частая причина — игра в
    ЭКСКЛЮЗИВНОМ полноэкранном режиме: Windows не даёт сторонней программе
    прочитать экран. Лечится переключением в Windowed / Borderless.
    """
    # 1) Windows API (только на Windows)
    if os.name == "nt":
        try:
            return _pixel_winapi(x, y)
        except Exception:
            pass  # упадём на запасной способ ниже
    # 2) Запасной способ через pyautogui (требует pyscreeze)
    try:
        px = pyautogui.pixel(int(x), int(y))
        return (int(px[0]), int(px[1]), int(px[2]))
    except Exception as e:
        raise RuntimeError(
            "Не удалось считать цвет пикселя. Чаще всего это значит, что "
            "игра запущена в ПОЛНОЭКРАННОМ (Fullscreen) режиме.\n"
            "    Переключи графику игры в 'Windowed Fullscreen' / 'Оконный "
            "без рамки' и попробуй снова.\n"
            f"    (техническая причина: {e})"
        )


def color_distance(c1, c2):
    """Евклидово расстояние между двумя RGB-цветами (0..~441)."""
    dr = c1[0] - c2[0]
    dg = c1[1] - c2[1]
    db = c1[2] - c2[2]
    return (dr * dr + dg * dg + db * db) ** 0.5


def same_color(current, reference):
    """Один и тот же ли это цвет (с поправкой на дрожание пикселя).

    Возвращает True, если текущий цвет совпадает с эталонным в пределах
    технического допуска COLOR_MATCH_TOLERANCE. Это и есть «такой же цвет»
    в практическом смысле: точного байт-в-байт равенства на экране не бывает.
    """
    return color_distance(current, reference) <= COLOR_MATCH_TOLERANCE


def press_with_delay(key):
    """Нажать клавишу со случайной задержкой ~человеческой реакции.

    Перед нажатием ждём случайное время в диапазоне DELAY_MIN_MS..DELAY_MAX_MS,
    плюс само удержание клавиши делаем не нулевым и тоже слегка случайным,
    чтобы интервалы не были идеально ровными.
    """
    delay = random.uniform(DELAY_MIN_MS, DELAY_MAX_MS) / 1000.0
    time.sleep(delay)
    hold = random.uniform(0.03, 0.08)  # удержание 30-80 мс
    keyboard.press(key)
    time.sleep(hold)
    keyboard.release(key)


def is_game_active():
    """True только если в фокусе именно окно Path of Exile 2.

    Проверяем по заголовку окна, а если получилось определить процесс —
    дополнительно по имени .exe. Если win32 недоступен, безопаснее НЕ жать
    фласки, чем жать их в постороннем окне, поэтому возвращаем False.
    """
    if not HAS_WIN32:
        return False

    hwnd = win32gui.GetForegroundWindow()
    if not hwnd:
        return False

    title = win32gui.GetWindowText(hwnd)
    title_ok = GAME_TITLE.lower() in title.lower()

    # Запасная/дополнительная проверка по имени процесса активного окна
    proc_ok = False
    if HAS_PSUTIL:
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            pname = psutil.Process(pid).name()
            proc_ok = GAME_PROCESS.lower() in pname.lower()
        except Exception:
            proc_ok = False

    # Достаточно совпадения по заголовку ИЛИ по процессу:
    # заголовок ловит большинство случаев, процесс — подстраховка,
    # если окно по какой-то причине без ожидаемого заголовка.
    return title_ok or proc_ok


def pick_point(label):
    """Наведи мышь на точку и нажми F8.

    Запоминаем и координаты, и ЦВЕТ пикселя в этот момент — он станет
    эталоном, с которым потом сравнивается текущий цвет.
    Возвращаем словарь {"xy": [x, y], "rgb": [r, g, b]}.
    """
    print(f"\n>>> Наведи курсор на {label}")
    print(f"    и нажми {PICK_KEY.upper()}")
    keyboard.wait(PICK_KEY)
    x, y = pyautogui.position()
    r, g, b = read_pixel(x, y)
    print(f"    OK: ({x}, {y})  эталонный цвет R={r} G={g} B={b}")
    time.sleep(0.4)  # чтобы одно нажатие не сработало дважды
    return {"xy": [int(x), int(y)], "rgb": [r, g, b]}


def pick_key(label, default):
    """Спросить клавишу: пользователь нажимает нужную кнопку на клавиатуре.

    Ждём ближайшее нажатие и берём имя клавиши. Игнорируем служебные
    PICK/PAUSE/RECALIBRATE, чтобы они не попали в назначение случайно.
    Enter оставляет значение по умолчанию.
    Возвращаем имя клавиши в нижнем регистре.
    """
    ignore = {PICK_KEY, PAUSE_KEY, RECALIBRATE_KEY}
    print(f"\n>>> Нажми клавишу для {label}")
    print(f"    (или Enter — оставить '{default}')")
    while True:
        event = keyboard.read_event(suppress=False)
        if event.event_type != "down":
            continue
        name = (event.name or "").lower()
        if not name:
            continue
        if name == "enter":
            print(f"    Оставлено: '{default}'")
            time.sleep(0.4)
            return default
        if name in ignore:
            # не даём назначить служебные клавиши программы
            print(f"    '{name}' зарезервирована программой, выбери другую.")
            continue
        if not valid_key(name):
            print(f"    '{name}' не подходит, попробуй другую клавишу.")
            continue
        print(f"    Назначено: '{name}'")
        time.sleep(0.4)  # чтобы это же нажатие не утекло дальше
        return name


def valid_key(key):
    """Проверяем, что клавиша из конфига вообще существует и пригодна.

    Принимаем непустую строку. Регистр не важен ('R' и 'r' равнозначны):
    keyboard понимает имена клавиш в нижнем регистре, поэтому проверяем
    приведённое к нижнему. Если клавиша неизвестна — вернём False.
    """
    if not isinstance(key, str) or not key.strip():
        return False
    k = key.strip().lower()
    try:
        # keyboard.key_to_scan_codes бросает исключение на неизвестной клавише
        keyboard.key_to_scan_codes(k)
        return True
    except Exception:
        return False


def valid_cooldown(value):
    """Кулдаун — неотрицательное число (секунды). Возвращаем (ok, float)."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False, 0.0
    if v < 0:
        return False, 0.0
    return True, v


def point_has_color(pt):
    """Точка в новом формате: словарь с 'xy' (2 числа) и 'rgb' (3 числа)?"""
    if not isinstance(pt, dict):
        return False
    xy = pt.get("xy")
    rgb = pt.get("rgb")
    return (isinstance(xy, list) and len(xy) == 2 and
            isinstance(rgb, list) and len(rgb) == 3)


def normalize_config(cfg):
    """Приводим конфиг к полному виду: гарантируем наличие точек и клавиш.

    Клавиши, если их нет или они некорректны, заменяем дефолтами и сообщаем.
    Возвращаем (нормализованный_конфиг, изменился_ли_он).
    Бросаем ValueError, если точки в старом формате без сохранённого цвета —
    тогда вызывающий запустит рекалибровку.
    """
    # Точки должны быть в новом формате с эталонным цветом
    for name in ("hp", "mana", "guard"):
        if not point_has_color(cfg.get(name)):
            raise ValueError(f"точка '{name}' без сохранённого цвета")

    changed = False

    hp_key = cfg.get("hp_key", DEFAULT_HP_KEY)
    if not valid_key(hp_key):
        print(f"[!] Клавиша HP '{hp_key}' некорректна, ставлю '{DEFAULT_HP_KEY}'.")
        hp_key = DEFAULT_HP_KEY
        changed = True

    mana_key = cfg.get("mana_key", DEFAULT_MANA_KEY)
    if not valid_key(mana_key):
        print(f"[!] Клавиша маны '{mana_key}' некорректна, ставлю '{DEFAULT_MANA_KEY}'.")
        mana_key = DEFAULT_MANA_KEY
        changed = True

    if "hp_key" not in cfg or "mana_key" not in cfg:
        changed = True  # дозапишем клавиши в старый конфиг без них

    # keyboard работает с именами клавиш в нижнем регистре, поэтому
    # храним и используем их так же. 'R' -> 'r', 'F' -> 'f' и т.п.
    hp_norm = hp_key.strip().lower()
    mana_norm = mana_key.strip().lower()
    if hp_norm != cfg.get("hp_key") or mana_norm != cfg.get("mana_key"):
        changed = True
    cfg["hp_key"] = hp_norm
    cfg["mana_key"] = mana_norm

    # Кулдауны (секунды), отдельно для HP и маны
    ok, hp_cd = valid_cooldown(cfg.get("hp_cooldown_sec", DEFAULT_HP_COOLDOWN))
    if not ok:
        print(f"[!] hp_cooldown_sec некорректен, ставлю {DEFAULT_HP_COOLDOWN}.")
        hp_cd = DEFAULT_HP_COOLDOWN
        changed = True
    ok, mana_cd = valid_cooldown(cfg.get("mana_cooldown_sec", DEFAULT_MANA_COOLDOWN))
    if not ok:
        print(f"[!] mana_cooldown_sec некорректен, ставлю {DEFAULT_MANA_COOLDOWN}.")
        mana_cd = DEFAULT_MANA_COOLDOWN
        changed = True
    if "hp_cooldown_sec" not in cfg or "mana_cooldown_sec" not in cfg:
        changed = True  # дозапишем кулдауны в конфиг без них
    cfg["hp_cooldown_sec"] = hp_cd
    cfg["mana_cooldown_sec"] = mana_cd

    return cfg, changed


def save_config(cfg):
    """Сохранить конфиг в файл рядом с программой."""
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        print(f"[!] Не удалось сохранить конфиг: {e}")
        return False


def calibrate():
    """Полная калибровка всех трёх точек. Клавиши берём из дефолтов
    (или из уже существующего конфига, если он есть)."""
    print("\n" + "=" * 50)
    print(" НАСТРОЙКА ТОЧЕК")
    print("=" * 50)
    print("Разверни игру так, чтобы был виден интерфейс,")
    print("и поочерёдно наведи курсор на каждую точку.")
    print()
    print("ВАЖНО про шары HP и маны:")
    print(" - выбирай точку на ЗАЛИТОЙ (полной) части шара, не на тёмной;")
    print(" - ВЫСОТА точки = момент срабатывания:")
    print("     точка ВЫШЕ  -> фласка при небольшой потере HP (рано);")
    print("     точка НИЖЕ  -> фласка когда HP почти кончилось (поздно).")
    print("   Жидкость в шаре опускается сверху вниз — фласка сработает,")
    print("   когда уровень опустится ниже выбранной точки.")

    hp = pick_point("ШАР ЗДОРОВЬЯ (на нужной высоте, по залитой части)")
    mana = pick_point("ШАР МАНЫ (на нужной высоте, по залитой части)")
    guard = pick_point("ЗОЛОТУЮ РАМКУ (яркий золотой участок)")

    # дефолты для подсказки: из старого конфига, если он есть, иначе общие
    def_hp, def_mana = DEFAULT_HP_KEY, DEFAULT_MANA_KEY
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                old = json.load(f)
            def_hp = old.get("hp_key", DEFAULT_HP_KEY)
            def_mana = old.get("mana_key", DEFAULT_MANA_KEY)
        except Exception:
            pass

    # выбор клавиш нажатием с клавиатуры
    print("\n" + "-" * 50)
    print(" НАЗНАЧЕНИЕ КЛАВИШ ФЛАСОК")
    print("-" * 50)
    hp_key = pick_key("ФЛАСКИ ЗДОРОВЬЯ", def_hp)
    mana_key = pick_key("ФЛАСКИ МАНЫ", def_mana)

    cfg = {
        "hp": hp,
        "mana": mana,
        "guard": guard,
        "hp_key": hp_key,
        "mana_key": mana_key,
    }
    cfg, _ = normalize_config(cfg)
    if save_config(cfg):
        print(f"\nНастройки сохранены: {path}")
        print(f"Клавиши: HP='{cfg['hp_key']}', мана='{cfg['mana_key']}' "
              f"(поменять можно здесь или повторной настройкой F10)")
    return cfg


def load_config():
    """Загрузить конфиг или запустить калибровку."""
    path = config_path()
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            # проверяем, что точки на месте
            for need in ("hp", "mana", "guard"):
                if need not in cfg:
                    raise ValueError(f"нет точки '{need}'")
            cfg, changed = normalize_config(cfg)
            if changed:
                save_config(cfg)  # дозапишем клавиши/исправления в файл
            hp_xy, hp_rgb = cfg["hp"]["xy"], cfg["hp"]["rgb"]
            mn_xy, mn_rgb = cfg["mana"]["xy"], cfg["mana"]["rgb"]
            gd_xy, gd_rgb = cfg["guard"]["xy"], cfg["guard"]["rgb"]
            print("Найдены сохранённые настройки:")
            print(f"  Здоровье: {hp_xy} цвет {hp_rgb} клавиша '{cfg['hp_key']}'")
            print(f"  Мана:     {mn_xy} цвет {mn_rgb} клавиша '{cfg['mana_key']}'")
            print(f"  Рамка:    {gd_xy} цвет {gd_rgb}")
            ans = input("\nИспользовать их? (Enter — да, R — заново): ").strip().lower()
            if ans == "r":
                return calibrate()
            return cfg
        except Exception as e:
            print(f"[!] Конфиг повреждён ({e}), нужна калибровка.")
            return calibrate()
    return calibrate()


def toggle_pause():
    global paused
    paused = not paused
    print("|| ПАУЗА" if paused else ">> РАБОТАЕТ")


def request_recalibrate():
    global need_recalibrate
    need_recalibrate = True


APP_TITLE = "System Resource Monitor"  # нейтральный заголовок окна консоли


def set_console_title(title):
    """Меняем заголовок окна консоли на нейтральный."""
    try:
        if os.name == "nt":
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def banner():
    print("=" * 50)
    print(f" {APP_TITLE}")
    print("=" * 50)
    if not HAS_WIN32:
        print("[!] Модуль win32 не найден — программа не может определить")
        print("    активное окно и работать не будет. Пересоберите с pywin32.")


def run_loop(config):
    global need_recalibrate
    hp_x, hp_y = config["hp"]["xy"]
    mana_x, mana_y = config["mana"]["xy"]
    guard_x, guard_y = config["guard"]["xy"]
    hp_ref = config["hp"]["rgb"]        # эталон полного шара здоровья
    mana_ref = config["mana"]["rgb"]    # эталон полного шара маны
    gold_ref = config["guard"]["rgb"]   # эталон яркого золота
    hp_key = config["hp_key"]
    mana_key = config["mana_key"]
    hp_cd = config["hp_cooldown_sec"]      # кулдаун HP, сек
    mana_cd = config["mana_cooldown_sec"]  # кулдаун маны, сек

    # время последнего срабатывания каждой фласки (monotonic).
    # 0.0 = ещё не жали, так что первая сработка не ждёт.
    last_hp = 0.0
    last_mana = 0.0

    keyboard.add_hotkey(PAUSE_KEY, toggle_pause)
    keyboard.add_hotkey(RECALIBRATE_KEY, request_recalibrate)

    print("\n" + "-" * 50)
    print(f"Запущено. Клавиши: HP='{hp_key}', мана='{mana_key}'.")
    print(f"Кулдаун: HP {hp_cd}с, мана {mana_cd}с.")
    print(f"{PAUSE_KEY.upper()} — пауза, "
          f"{RECALIBRATE_KEY.upper()} — перенастроить точки.")
    print("Закрой окно или Ctrl+C для выхода.")
    print("-" * 50)

    while True:
        if need_recalibrate:
            need_recalibrate = False
            keyboard.clear_all_hotkeys()
            new_cfg = calibrate()
            return new_cfg  # вернёмся и перезапустим цикл с новыми точками

        if not paused and is_game_active():
            try:
                gold_now = read_pixel(guard_x, guard_y)
                # Контрольный пиксель: если цвет НЕ совпадает с эталоном
                # (меню затемнило экран) — ничего не делаем.
                if same_color(gold_now, gold_ref):
                    now = time.monotonic()

                    # HP: шар опустел И прошёл кулдаун HP -> жмём.
                    hp_now = read_pixel(hp_x, hp_y)
                    if (not same_color(hp_now, hp_ref)
                            and now - last_hp >= hp_cd):
                        press_with_delay(hp_key)
                        last_hp = time.monotonic()
                        print(f"[{time.strftime('%H:%M:%S')}] HP -> фласка "
                              f"(клавиша '{hp_key}')")

                    # Мана: независимо от HP, свой кулдаун.
                    mana_now = read_pixel(mana_x, mana_y)
                    if (not same_color(mana_now, mana_ref)
                            and now - last_mana >= mana_cd):
                        press_with_delay(mana_key)
                        last_mana = time.monotonic()
                        print(f"[{time.strftime('%H:%M:%S')}] Мана -> фласка "
                              f"(клавиша '{mana_key}')")
            except Exception:
                # Разовый сбой чтения экрана (например, переключение режима)
                # не должен ронять программу — просто пропускаем тик.
                pass

        time.sleep(CHECK_INTERVAL)


def main():
    set_console_title(APP_TITLE)
    banner()
    try:
        config = load_config()
        while True:
            config = run_loop(config)  # run_loop возвращает новый конфиг при F10
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except Exception as e:
        # Любая иная ошибка: показываем её и НЕ закрываем окно сразу,
        # чтобы пользователь успел прочитать причину.
        print("\n" + "!" * 60)
        print("ОШИБКА. Программа остановлена.")
        print("!" * 60)
        print(str(e))
        print("\nЕсли тут написано про полноэкранный режим — переключи игру")
        print("в 'Windowed Fullscreen' (оконный без рамки) и запусти снова.")
    finally:
        try:
            input("\nНажми Enter, чтобы закрыть окно...")
        except Exception:
            pass


if __name__ == "__main__":
    main()
