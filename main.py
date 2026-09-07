import sys
import os
import json
import queue
import threading
import time
import traceback
import csv
from datetime import datetime

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import customtkinter as ctk
import serial
import serial.tools.list_ports
from tkinter import filedialog
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

BAUD_RATE = 115200
VOLTAGE_CONST = 24.4  # Константное напряжение (В)


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Стенд тестирования моторов")
        self.geometry("1100x890")

        self.serial_port = None
        self.reader_thread = None
        self.stop_reader = threading.Event()
        self.rx_queue = queue.Queue()
        self.tx_queue = queue.Queue()
        self.is_running = False

        # Переменные для разгона и газа
        self.current_pwm = 1000
        self.current_pct = 0
        self.test_thread = None
        self.ramp_delay = 0.3  # Значение задержки по умолчанию (300 мс)
        self.arm_delay = 2.0   # Время удержания на минимальном газу перед разгоном (сек)
        self.delayed_start_pending = False
        self.delayed_start_after_id = None

        # Защита от частой отправки команд с ползунка (debounce)
        self.last_manual_pwm_time = 0.0

        # Хранение последнего известного значения температуры
        self.last_known_temp = 0.0

        self.data = {"t": [], "pwm": [], "throttle_pct": [], "rpm": [],
                     "voltage": [], "current_a": [], "power": [], "thrust": [], "temp": []}

        self._build_ui()
        self._refresh_ports()
        self.after(50, self._poll_queue)

    def _build_ui(self):
        # Панель подключения
        top = ctk.CTkFrame(self)
        top.pack(fill="x", padx=16, pady=(16, 8))

        self.port_combo = ctk.CTkComboBox(top, values=[], width=220)
        self.port_combo.pack(side="left", padx=(0, 8))

        ctk.CTkButton(top, text="Обновить порты", width=130,
                      command=self._refresh_ports).pack(side="left", padx=4)

        self.btn_connect = ctk.CTkButton(top, text="Подключить", width=120,
                                         command=self.connect)
        self.btn_connect.pack(side="left", padx=4)

        self.label_status = ctk.CTkLabel(self, text="Не подключено",
                                         font=ctk.CTkFont(size=14, weight="bold"))
        self.label_status.pack(pady=(4, 8))

        # --- НАСТРОЙКА ПЛАВНОСТИ (ПОЛЗУНОК АВТО-РАЗГОНА) ---
        slider_frame = ctk.CTkFrame(self)
        slider_frame.pack(fill="x", padx=16, pady=4)

        self.label_slider_title = ctk.CTkLabel(slider_frame, text="Задержка шага разгона: 300 мс (Очень плавно)",
                                               font=ctk.CTkFont(size=12))
        self.label_slider_title.pack(pady=(4, 0))

        self.speed_slider = ctk.CTkSlider(slider_frame, from_=50, to=1000, number_of_steps=19,
                                          command=self._on_slider_move)
        self.speed_slider.set(300)
        self.speed_slider.pack(fill="x", padx=20, pady=(0, 6))

        self.label_throttle = ctk.CTkLabel(self, text="Газ: 0% (PWM: 1000 мкс)",
                                           font=ctk.CTkFont(size=30, weight="bold"))
        self.label_throttle.pack(pady=4)

        # --- РУЧНОЕ УПРАВЛЕНИЕ ГАЗОМ И ОХЛАЖДЕНИЕ ---
        manual_frame = ctk.CTkFrame(self)
        manual_frame.pack(fill="x", padx=16, pady=4)

        self.label_manual_pwm = ctk.CTkLabel(manual_frame, text="Ручная регулировка газа (1000 — 2000 мкс):",
                                             font=ctk.CTkFont(size=12, weight="bold"))
        self.label_manual_pwm.pack(pady=(4, 0))

        manual_inner_frame = ctk.CTkFrame(manual_frame, fg_color="transparent")
        manual_inner_frame.pack(fill="x", padx=10, pady=4)

        self.pwm_slider = ctk.CTkSlider(manual_inner_frame, from_=1000, to=2000, number_of_steps=200,
                                        command=self._on_pwm_slider_move, state="disabled")
        self.pwm_slider.set(1000)
        self.pwm_slider.pack(side="left", expand=True, fill="x", padx=(10, 10))

        self.btn_cool = ctk.CTkButton(manual_inner_frame, text="ОХЛАЖДЕНИЕ (10%)",
                                     fg_color="#008080", hover_color="#005656", width=180, height=32,
                                     font=ctk.CTkFont(size=12, weight="bold"),
                                     command=self.set_cooling_mode, state="disabled")
        self.btn_cool.pack(side="right", padx=(0, 10))

        # Панель приборов
        stats = ctk.CTkFrame(self)
        stats.pack(fill="x", padx=16, pady=8)
        self.stat_labels = {}
        for i, (key, title) in enumerate([
            ("rpm", "RPM"), ("voltage", "Вольты (В)"),
            ("current_a", "Ампер (А)"), ("power", "Мощность (Вт)"), ("temp", "Темп. (°C)"), ("thrust", "Тяга")
        ]):
            f = ctk.CTkFrame(stats)
            f.grid(row=0, column=i, padx=6, pady=8, sticky="nsew")
            stats.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=11)).pack(pady=(6, 0))
            val = ctk.CTkLabel(f, text="0", font=ctk.CTkFont(size=20, weight="bold"))
            val.pack(pady=(0, 6))
            self.stat_labels[key] = val

        # Управление тестом
        btns = ctk.CTkFrame(self)
        btns.pack(fill="x", padx=16, pady=8)
        self.btn_start = ctk.CTkButton(btns, text="НАЧАТЬ ИСПЫТАНИЯ", fg_color="green",
                                       hover_color="darkgreen", height=50,
                                       font=ctk.CTkFont(size=14, weight="bold"),
                                       command=self.start_test, state="disabled")
        self.btn_start.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.btn_stop = ctk.CTkButton(btns, text="АВАРИЙНЫЙ СТОП", fg_color="red",
                                     hover_color="darkred", height=50,
                                     font=ctk.CTkFont(size=16, weight="bold"),
                                     command=self.emergency_stop, state="disabled")
        self.btn_stop.pack(side="left", expand=True, fill="x", padx=(8, 0))

        # --- ОТЛОЖЕННЫЙ СТАРТ ---
        delayed_frame = ctk.CTkFrame(self)
        delayed_frame.pack(fill="x", padx=16, pady=(0, 8))

        self.label_delayed_slider = ctk.CTkLabel(delayed_frame, text="Задержка старта: 5 с",
                                                 font=ctk.CTkFont(size=12))
        self.label_delayed_slider.pack(pady=(6, 0))

        self.delayed_slider = ctk.CTkSlider(delayed_frame, from_=1, to=60, number_of_steps=59,
                                            command=self._on_delayed_slider_move)
        self.delayed_slider.set(5)
        self.delayed_slider.pack(fill="x", padx=20, pady=(0, 6))

        self.btn_delayed_start = ctk.CTkButton(delayed_frame, text="ОТЛОЖЕННЫЙ СТАРТ",
                                               fg_color="#8a6d00", hover_color="#6b5400",
                                               height=40,
                                               font=ctk.CTkFont(size=13, weight="bold"),
                                               command=self.start_delayed_test, state="disabled")
        self.btn_delayed_start.pack(fill="x", padx=20, pady=(0, 8))

        # --- КНОПКИ CSV И СБРОСА (ПЕРЕЗАПУСКА) ---
        export_frame = ctk.CTkFrame(self, fg_color="transparent")
        export_frame.pack(fill="x", padx=16, pady=(0, 8))

        self.btn_csv = ctk.CTkButton(export_frame, text="Сохранить CSV как...", command=self.export_csv,
                                     state="disabled")
        self.btn_csv.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self.btn_reset = ctk.CTkButton(export_frame, text="Сброс / Готовность к новому тесту",
                                       fg_color="#4a4a4a", hover_color="#333333",
                                       command=self.reset_app)
        self.btn_reset.pack(side="right", expand=True, fill="x", padx=(4, 0))

        # Графики
        self.fig = Figure(figsize=(10, 4.5), dpi=100, facecolor="#1e1e1e")
        self.axs = self.fig.subplots(2, 3)
        self._draw_empty_charts()
                
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(0, 8))

        # Терминал логов
        self.log_box = ctk.CTkTextbox(self, height=80, font=ctk.CTkFont(size=11))
        self.log_box.pack(fill="x", padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")

    def _draw_empty_charts(self):
        titles = [
            ("0,0", "Напряжение (В)"), ("0,1", "Ток (А)"), ("0,2", "Мощность (Вт)"),
            ("1,0", "Тяга"), ("1,1", "Обороты (RPM)"), ("1,2", "Темп. (°C)")
        ]
        idx = 0
        for r in range(2):
            for c in range(3):
                ax = self.axs[r, c]
                ax.clear()
                ax.set_facecolor("#1e1e1e")
                ax.set_title(titles[idx][1], color="white", fontsize=10)
                ax.tick_params(colors="white", labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color("white")
                idx += 1

    def reset_app(self):
        """Полный безопасный сброс состояния без зависаний и утечек ресурсов"""
        self._cancel_delayed_start()

        # Остановка активных потоков
        self.is_running = False
        self.current_pwm = 1000
        self.current_pct = 0
        self.pwm_slider.set(1000)

        # Отправка команды останова в контроллер
        if self.serial_port:
            try:
                self.tx_queue.put(b"STOP\n")
            except Exception:
                pass

        # Безопасная очистка массивов данных
        for k in self.data:
            self.data[k].clear()

        # Полный перезапуск холста графиков
        self._draw_empty_charts()
        self.canvas.draw_idle()

        # Восстановление показателей приборов
        for lbl in self.stat_labels.values():
            lbl.configure(text="0")

        # Настройка кнопок
        self.label_throttle.configure(text="Газ: 0% (PWM: 1000 мкс)")
        if self.serial_port:
            self.label_status.configure(text="Готов к новому испытанию", text_color="lightgreen")
            self.btn_start.configure(state="normal")
            self.btn_delayed_start.configure(state="normal")
            self.pwm_slider.configure(state="normal")
            self.btn_cool.configure(state="normal")
        else:
            self.label_status.configure(text="Не подключено", text_color="orange")
            self.btn_start.configure(state="disabled")
            self.btn_delayed_start.configure(state="disabled")

        self.btn_stop.configure(state="disabled")
        self.speed_slider.configure(state="normal")
        self.delayed_slider.configure(state="normal")
        self.btn_csv.configure(state="disabled")

        self.log("=== ВЫПОЛНЕН СБРОС: Приложение готово к новому тесту ===")

    def _on_slider_move(self, value):
        ms_val = int(value)
        self.ramp_delay = ms_val / 1000.0

        if ms_val <= 100:
            desc = "(Быстро)"
        elif ms_val <= 400:
            desc = "(Очень плавно)"
        else:
            desc = "(Экстремально медленно)"

        self.label_slider_title.configure(text=f"Задержка шага разгона: {ms_val} мс {desc}")

    def _on_delayed_slider_move(self, value):
        sec_val = int(round(value))
        self.label_delayed_slider.configure(text=f"Задержка старта: {sec_val} с")

    def _on_pwm_slider_move(self, value):
        pwm_val = int(round(value))
        self.current_pwm = pwm_val
        self.current_pct = int((pwm_val - 1000) / 10)

        self.label_throttle.configure(
            text=f"Газ: {self.current_pct}% (PWM: {self.current_pwm} мкс)"
        )

        now = time.time()
        if now - self.last_manual_pwm_time > 0.03:
            self.last_manual_pwm_time = now
            if self.serial_port:
                self.tx_queue.put(f"PWM:{pwm_val}\n".encode())

    def set_cooling_mode(self):
        if not self.serial_port or self.is_running:
            return

        cool_pwm = 1100
        self.current_pwm = cool_pwm
        self.current_pct = 10
        self.pwm_slider.set(cool_pwm)

        self.label_throttle.configure(
            text=f"Газ: {self.current_pct}% (PWM: {self.current_pwm} мкс)"
        )
        self.label_status.configure(text="Режим охлаждения (10% газа)", text_color="cyan")
        self.log("Включен холостой ход / охлаждение (10% газа)")

        self.tx_queue.put(b"IDLE\n")

    def log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"{datetime.now().strftime('%H:%M:%S')}  {msg}\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _refresh_ports(self):
        try:
            ports = [p.device for p in serial.tools.list_ports.comports()]
        except Exception as e:
            self.log(f"Ошибка поиска портов: {e}")
            ports = []
        self.port_combo.configure(values=ports)
        if ports:
            self.port_combo.set(ports[0])
            self.log(f"Найдены порты: {', '.join(ports)}")
        else:
            self.log("Порты не найдены (список пуст)")

    def connect(self):
        port = self.port_combo.get()
        if not port:
            self.label_status.configure(text="Выбери порт", text_color="orange")
            return
        self.btn_connect.configure(state="disabled")
        self.label_status.configure(text=f"Подключение к {port}...", text_color="orange")
        threading.Thread(target=self._connect_worker, args=(port,), daemon=True).start()

    def _connect_worker(self, port):
        try:
            sp = serial.Serial(port, BAUD_RATE, timeout=0.2)
            time.sleep(2)
            self.serial_port = sp
            self.stop_reader.clear()
            self.reader_thread = threading.Thread(target=self._io_loop, daemon=True)
            self.reader_thread.start()
            self.rx_queue.put(json.dumps({"__connected__": True, "port": port}))
        except Exception as e:
            self.rx_queue.put(json.dumps({"__connect_error__": str(e)}))

    def _io_loop(self):
        while not self.stop_reader.is_set():
            try:
                while True:
                    cmd = self.tx_queue.get_nowait()
                    if self.serial_port and self.serial_port.is_open:
                        self.serial_port.write(cmd)
            except queue.Empty:
                pass
            except Exception as e:
                self.rx_queue.put(json.dumps({"__io_error__": f"запись: {e}"}))

            try:
                if self.serial_port and self.serial_port.is_open:
                    line = self.serial_port.readline().decode("utf-8", errors="ignore").strip()
                    if line:
                        self.rx_queue.put(line)
            except Exception as e:
                self.rx_queue.put(json.dumps({"__io_error__": f"чтение: {e}"}))
                break

    def _poll_queue(self):
        try:
            while True:
                try:
                    line = self.rx_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_line(line)
                except Exception as e:
                    self.log(f"Ошибка разбора строки '{line}': {e}")
        except Exception as e:
            self.log(f"Ошибка в цикле обновления: {e}\n{traceback.format_exc()}")
        finally:
            self.after(50, self._poll_queue)

    def start_test(self):
        if self.is_running or not self.serial_port:
            return
        for k in self.data:
            self.data[k].clear()
        self.btn_csv.configure(state="disabled")

        self.is_running = True
        self.current_pwm = 1000
        self.current_pct = 0
        self.pwm_slider.set(1000)

        self.speed_slider.configure(state="disabled")
        self.delayed_slider.configure(state="disabled")
        self.btn_delayed_start.configure(state="disabled")
        self.pwm_slider.configure(state="disabled")
        self.btn_cool.configure(state="disabled")

        self.label_status.configure(text="Армирование ESC...", text_color="yellow")
        self.log(f"Запуск теста. Армирование {self.arm_delay:.1f} с на минимальном газу, "
                 f"затем плавный разгон шагом 1 мкс ШИМ каждые {int(self.ramp_delay * 1000)} мс")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.test_thread = threading.Thread(target=self._smooth_ramp_worker, daemon=True)
        self.test_thread.start()

    def start_delayed_test(self):
        if self.is_running or self.delayed_start_pending or not self.serial_port:
            return

        delay_sec = int(round(self.delayed_slider.get()))
        self.delayed_start_pending = True

        self.btn_start.configure(state="disabled")
        self.btn_delayed_start.configure(state="disabled")
        self.delayed_slider.configure(state="disabled")
        self.speed_slider.configure(state="disabled")
        self.pwm_slider.configure(state="disabled")
        self.btn_cool.configure(state="disabled")
        self.btn_stop.configure(state="normal")

        self.log(f"Отложенный старт: тест начнётся через {delay_sec} с")
        self._delayed_start_tick(delay_sec)

    def _delayed_start_tick(self, remaining):
        if not self.delayed_start_pending:
            return

        if remaining <= 0:
            self.delayed_start_pending = False
            self.delayed_start_after_id = None
            self.start_test()
            return

        self.label_status.configure(text=f"Отложенный старт: {remaining} с...", text_color="yellow")
        self.delayed_start_after_id = self.after(1000, lambda: self._delayed_start_tick(remaining - 1))

    def _cancel_delayed_start(self):
        if not self.delayed_start_pending:
            return False
        self.delayed_start_pending = False
        if self.delayed_start_after_id is not None:
            try:
                self.after_cancel(self.delayed_start_after_id)
            except Exception:
                pass
            self.delayed_start_after_id = None
        self.label_status.configure(text="Отложенный старт отменён", text_color="orange")
        self.btn_start.configure(state="normal")
        self.btn_delayed_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.speed_slider.configure(state="normal")
        self.delayed_slider.configure(state="normal")
        self.pwm_slider.configure(state="normal")
        self.btn_cool.configure(state="normal")
        return True

    def _smooth_ramp_worker(self):
        self.tx_queue.put(b"START\n")
        self.tx_queue.put(f"PWM:{self.current_pwm}\n".encode())
        self.rx_queue.put(json.dumps({"status": "ARMING"}))

        armed_until = time.time() + self.arm_delay
        while self.is_running and time.time() < armed_until:
            time.sleep(0.05)

        if not self.is_running:
            return

        self.rx_queue.put(json.dumps({"status": "RAMPING"}))

        while self.is_running and self.current_pwm < 2000:
            time.sleep(self.ramp_delay)
            if not self.is_running:
                break

            self.current_pwm += 1
            self.current_pct = int((self.current_pwm - 1000) / 10)

            cmd_str = f"PWM:{self.current_pwm}\n".encode()
            self.tx_queue.put(cmd_str)

        if self.is_running and self.current_pwm >= 2000:
            self.is_running = False
            self.tx_queue.put(b"STOP\n")
            self.rx_queue.put(json.dumps({"status": "DONE"}))

    def emergency_stop(self):
        if self._cancel_delayed_start():
            return

        self.is_running = False
        self.current_pwm = 1000
        self.current_pct = 0
        self.pwm_slider.set(1000)

        if self.serial_port:
            self.tx_queue.put(b"STOP\n")
        self.label_status.configure(text="ТЕСТ ПРИНУДИТЕЛЬНО ПРЕРВАН", text_color="red")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.speed_slider.configure(state="normal")
        self.delayed_slider.configure(state="normal")
        self.btn_delayed_start.configure(state="normal")
        self.pwm_slider.configure(state="normal")
        self.btn_cool.configure(state="normal")

    def _handle_line(self, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            self.log(f"Не JSON (проигнорировано): {line}")
            return

        if "__connected__" in obj:
            self.label_status.configure(text=f"Подключено: {obj['port']}", text_color="lightgreen")
            self.btn_start.configure(state="normal")
            self.btn_delayed_start.configure(state="normal")
            self.pwm_slider.configure(state="normal")
            self.btn_cool.configure(state="normal")
            self.log(f"Порт {obj['port']} открыт")
            return

        if "__connect_error__" in obj:
            self.label_status.configure(text=f"Ошибка подключения: {obj['__connect_error__']}", text_color="red")
            self.btn_connect.configure(state="normal")
            self.log(f"Ошибка подключения: {obj['__connect_error__']}")
            return

        if "__io_error__" in obj:
            self.log(f"Ошибка порта: {obj['__io_error__']}")
            return

        if "status" in obj:
            status = obj["status"]
            reason = obj.get("reason", "")
            self.log(f"Статус: {status}" + (f" ({reason})" if reason else ""))

            if status == "BOOT_OK":
                self.label_status.configure(text="Прошивка загрузилась, готова к работе", text_color="lightgreen")
                return
            if status == "WARN":
                return
            if status == "ARMING":
                self.label_status.configure(text="Армирование ESC на минимальном газу...", text_color="yellow")
                return
            if status == "RAMPING":
                self.label_status.configure(text="Идёт плавный разгон...", text_color="yellow")
                return
            if status == "COOLING_IDLE":
                self.label_status.configure(text="Охлаждение: 10% газа", text_color="cyan")
                return

            if status == "DONE":
                self.label_status.configure(text="Тест успешно завершён", text_color="lightgreen")
            elif status == "STOPPED":
                self.label_status.configure(text="Остановлено пользователем", text_color="orange")
            elif status == "SAFETY_CUTOFF":
                self.label_status.configure(text=f"Защита сработала: {reason}", text_color="red")

            self.is_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            self.speed_slider.configure(state="normal")
            self.delayed_slider.configure(state="normal")
            self.btn_delayed_start.configure(state="normal")
            self.pwm_slider.configure(state="normal")
            self.btn_cool.configure(state="normal")
            if self.data["t"]:
                self.btn_csv.configure(state="normal")
            return

        # --- ОБРАБОТКА ТЕМПЕРАТУРЫ ---
        temp_val = None
        for k in ("temp", "temperature", "temp_c", "t_deg"):
            if k in obj:
                try:
                    temp_val = float(obj[k])
                    break
                except (ValueError, TypeError):
                    pass

        if temp_val is not None:
            self.last_known_temp = temp_val

        obj["temp"] = self.last_known_temp

        # --- НАПРЯЖЕНИЕ И МОЩНОСТЬ (P = U * I) ---
        u = VOLTAGE_CONST
        obj["voltage"] = u

        try:
            i = float(obj.get("current_a", 0.0))
        except (ValueError, TypeError):
            i = 0.0

        obj["power"] = u * i

        # Маппинг времени
        if "ms" in obj and "t" not in obj:
            obj["t"] = obj["ms"] / 1000.0
        elif "t" not in obj:
            obj["t"] = time.time()

        if "pwm" in obj and not self.is_running:
            self.current_pwm = int(obj["pwm"])
            self.current_pct = int((self.current_pwm - 1000) / 10)

        obj['pwm'] = self.current_pwm
        obj['throttle_pct'] = self.current_pct

        if self.is_running:
            for k in self.data:
                val = obj.get(k, 0.0)
                self.data[k].append(val)
            self._update_chart()

        self.label_throttle.configure(
            text=f"Газ: {self.current_pct}% (PWM: {self.current_pwm} мкс)"
        )

        for k, lbl in self.stat_labels.items():
            if k in obj:
                val = obj[k]
                if k in ("voltage", "current_a", "power", "temp") and isinstance(val, (int, float)):
                    if k in ("temp", "power"):
                        lbl.configure(text=f"{val:.1f}")
                    else:
                        lbl.configure(text=f"{val:.2f}")
                else:
                    lbl.configure(text=f"{val:.0f}" if isinstance(val, (int, float)) else str(val))

    def _update_chart(self):
        x = self.data["throttle_pct"]
        plots = [
            (self.axs[0, 0], self.data["voltage"], "Напряжение (В)", "#ffb84f"),
            (self.axs[0, 1], self.data["current_a"], "Ток (А)", "#ff4d4f"),
            (self.axs[0, 2], self.data["power"], "Мощность (Вт)", "#e84393"),
            (self.axs[1, 0], self.data["thrust"], "Тяга", "#4fff8f"),
            (self.axs[1, 1], self.data["rpm"], "Обороты (RPM)", "#4f9dff"),
            (self.axs[1, 2], self.data["temp"], "Темп. (°C)", "#00cec9"),
        ]
        for ax, y, title, color in plots:
            if len(x) == len(y) and len(x) > 0:
                ax.clear()
                ax.plot(x, y, color=color, marker="o", markersize=3)
                ax.set_title(title, color="white", fontsize=10)
                ax.set_facecolor("#1e1e1e")
                ax.tick_params(colors="white", labelsize=8)
                for spine in ax.spines.values():
                    spine.set_color("white")
        self.canvas.draw_idle()

    def export_csv(self):
        if not self.data["t"]:
            self.log("Нет данных для сохранения")
            return

        default_filename = f"motor_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

        filepath = filedialog.asksaveasfilename(
            initialfile=default_filename,
            defaultextension=".csv",
            filetypes=[("CSV файлы", "*.csv"), ("Все файлы", "*.*")],
            title="Выберите папку для сохранения результатов теста"
        )

        if not filepath:
            self.log("Сохранение отменено пользователем")
            return

        try:
            keys = list(self.data.keys())
            min_len = min(len(self.data[k]) for k in keys)

            with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f, delimiter=";")
                writer.writerow(keys)
                for i in range(min_len):
                    writer.writerow([self.data[k][i] for k in keys])

            just_name = os.path.basename(filepath)
            self.label_status.configure(text=f"Сохранено: {just_name}", text_color="lightgreen")
            self.log(f"Файл успешно сохранен в: {filepath}")
        except Exception as e:
            self.log(f"Ошибка записи файла: {e}\n{traceback.format_exc()}")
            self.label_status.configure(text="Ошибка при сохранении файла!", text_color="red")


if __name__ == "__main__":
    app = App()
    app.mainloop()
