#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Автокликер QTE для тренажёрного зала Escape from Tarkov.

Важно: используйте на свой риск.
"""

import ctypes
import random
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import mss
import numpy as np
import pyautogui
from pynput import keyboard

# =========================
# CONFIG
# =========================
CONFIG = {
    # Горячие клавиши
    "start_hotkey": keyboard.Key.f8,
    "stop_hotkey": keyboard.Key.f9,

    # Режим работы: 1 = таймер, 2 = OpenCV
    "mode": 2,

    # Паузы и лимиты
    "waves_per_session": 15,
    "post_session_pause_sec": 8.0,
    "poll_interval_sec": 0.01,

    # Рандомизация времени клика
    "click_jitter_sec": 0.2,

    # ---------- MODE 1 ----------
    # Через сколько секунд после начала волны кликать
    "mode1_click_delay_sec": 1.45,
    # Ожидание следующей волны после клика
    "mode1_between_waves_sec": 1.2,

    # ---------- MODE 2 ----------
    # ROI области мини-игры (подставьте свои координаты)
    # Для 1920x1080 примерные значения внизу по центру:
    "roi": {
        "left": 760,
        "top": 730,
        "width": 400,
        "height": 260,
    },
    # Минимальный/максимальный радиус для поиска малого круга
    "min_radius": 10,
    "max_radius": 160,
    # Допуск совпадения радиусов большого и малого шестиугольника
    "radius_match_tolerance": 3.5,
    # Защита от двойного клика в одной волне
    "min_click_gap_sec": 0.45,

    # Цветовой детект (дополнительно): зелёная рамка в момент совпадения
    # HSV диапазон зелёного
    "use_green_confirmation": True,
    "green_hsv_lower": (35, 60, 60),
    "green_hsv_upper": (90, 255, 255),
    # Минимум зелёных пикселей в ROI для подтверждения
    "green_pixels_threshold": 80,

    # Безопасность: кликать только если активное окно содержит фразу ниже
    "required_window_title_substring": "Escape from Tarkov",

    # Логирование
    "verbose": True,
}


@dataclass
class State:
    running: bool = False
    stop_requested: bool = False
    wave_index: int = 0
    last_click_ts: float = 0.0


class TarkovGymQTEBot:
    def __init__(self, config: dict):
        self.cfg = config
        self.state = State()
        self.worker: Optional[threading.Thread] = None
        self.lock = threading.Lock()
        self.sct = mss.mss()

    # ---------- Утилиты ----------
    def log(self, msg: str) -> None:
        if self.cfg["verbose"]:
            print(f"[{time.strftime('%H:%M:%S')}] {msg}")

    def is_tarkov_focused(self) -> bool:
        """Проверка активного окна (Windows)."""
        if not hasattr(ctypes, "windll"):
            # Не Windows — безопасно запрещаем клики.
            return False

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if hwnd == 0:
            return False

        length = user32.GetWindowTextLengthW(hwnd)
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value
        return self.cfg["required_window_title_substring"].lower() in title.lower()

    def do_safe_click(self, wave_num: int, reason: str) -> bool:
        """Кликаем только если окно Таркова в фокусе."""
        if not self.is_tarkov_focused():
            self.log(f"Волна {wave_num}: пропуск клика (не в фокусе Tarkov)")
            return False

        pyautogui.click(button="left")
        self.state.last_click_ts = time.time()
        self.log(f"Волна {wave_num}: клик выполнен ({reason})")
        return True

    # ---------- MODE 1 ----------
    def run_mode1_session(self) -> None:
        self.log("Старт серии (режим 1: таймер)")
        for wave in range(1, self.cfg["waves_per_session"] + 1):
            if self.state.stop_requested:
                self.log("Остановлено пользователем")
                return

            self.state.wave_index = wave
            delay = self.cfg["mode1_click_delay_sec"] + random.uniform(
                -self.cfg["click_jitter_sec"], self.cfg["click_jitter_sec"]
            )
            delay = max(0.0, delay)

            self.log(f"Волна {wave}: ожидание {delay:.3f} сек до клика")
            time.sleep(delay)
            self.do_safe_click(wave, reason="таймер")
            time.sleep(self.cfg["mode1_between_waves_sec"])

        self.log("Серия завершена (15/15)")

    # ---------- MODE 2 ----------
    def capture_roi(self) -> np.ndarray:
        roi = self.cfg["roi"]
        frame = np.array(self.sct.grab(roi))
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2BGR)

    def detect_radii(self, frame: np.ndarray) -> tuple[Optional[float], Optional[float]]:
        """Ищет 2 наиболее вероятных контура (большой и малый)."""
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 80, 160)

        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        radii = []

        for cnt in contours:
            area = cv2.contourArea(cnt)
            if area < 80:
                continue
            (x, y), r = cv2.minEnclosingCircle(cnt)
            if self.cfg["min_radius"] <= r <= self.cfg["max_radius"]:
                radii.append(r)

        if len(radii) < 2:
            return None, None

        radii.sort(reverse=True)
        big = radii[0]
        small = radii[1]
        return big, small

    def green_confirm(self, frame: np.ndarray) -> bool:
        if not self.cfg["use_green_confirmation"]:
            return True

        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(
            hsv,
            np.array(self.cfg["green_hsv_lower"], dtype=np.uint8),
            np.array(self.cfg["green_hsv_upper"], dtype=np.uint8),
        )
        green_pixels = int(np.count_nonzero(mask))
        return green_pixels >= self.cfg["green_pixels_threshold"]

    def run_mode2_session(self) -> None:
        self.log("Старт серии (режим 2: OpenCV)")
        wave = 0
        while wave < self.cfg["waves_per_session"]:
            if self.state.stop_requested:
                self.log("Остановлено пользователем")
                return

            frame = self.capture_roi()
            big_r, small_r = self.detect_radii(frame)
            if big_r is None or small_r is None:
                time.sleep(self.cfg["poll_interval_sec"])
                continue

            radius_diff = abs(big_r - small_r)
            now = time.time()
            can_click = (now - self.state.last_click_ts) >= self.cfg["min_click_gap_sec"]

            if radius_diff <= self.cfg["radius_match_tolerance"] and can_click:
                jitter = random.uniform(-self.cfg["click_jitter_sec"], self.cfg["click_jitter_sec"])
                if jitter > 0:
                    time.sleep(jitter)

                frame2 = self.capture_roi()
                if self.green_confirm(frame2):
                    wave += 1
                    self.state.wave_index = wave
                    self.do_safe_click(wave, reason=f"OpenCV, Δr={radius_diff:.2f}")
                else:
                    self.log("Совпадение радиуса есть, но зелёное подтверждение не прошло")

            time.sleep(self.cfg["poll_interval_sec"])

        self.log("Серия завершена (15/15)")

    # ---------- Управление ----------
    def worker_loop(self) -> None:
        self.log("Ожидание сессий. F9 для остановки.")
        while not self.state.stop_requested:
            if self.cfg["mode"] == 1:
                self.run_mode1_session()
            else:
                self.run_mode2_session()

            if self.state.stop_requested:
                break

            self.log(f"Пауза {self.cfg['post_session_pause_sec']} сек до следующей тренировки")
            slept = 0.0
            while slept < self.cfg["post_session_pause_sec"] and not self.state.stop_requested:
                time.sleep(0.1)
                slept += 0.1

        with self.lock:
            self.state.running = False
        self.log("Поток работы завершён")

    def start(self) -> None:
        with self.lock:
            if self.state.running:
                self.log("Скрипт уже запущен")
                return
            self.state.stop_requested = False
            self.state.running = True

        self.worker = threading.Thread(target=self.worker_loop, daemon=True)
        self.worker.start()
        self.log("Скрипт запущен")

    def stop(self) -> None:
        with self.lock:
            if not self.state.running:
                self.log("Скрипт не запущен")
                return
            self.state.stop_requested = True

        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3.0)
        with self.lock:
            self.state.running = False
        self.log("Скрипт остановлен")


def main() -> None:
    bot = TarkovGymQTEBot(CONFIG)
    print("\n=== Tarkov Gym QTE Bot ===")
    print(f"Старт: {CONFIG['start_hotkey']} | Стоп: {CONFIG['stop_hotkey']}")
    print("Нажмите ESC для полного выхода.\n")

    def on_press(key):
        if key == CONFIG["start_hotkey"]:
            bot.start()
        elif key == CONFIG["stop_hotkey"]:
            bot.stop()
        elif key == keyboard.Key.esc:
            bot.stop()
            return False
        return True

    with keyboard.Listener(on_press=on_press) as listener:
        listener.join()

    print("Выход из программы.")


if __name__ == "__main__":
    pyautogui.FAILSAFE = False
    main()
