"""
Motor Test Stand — настольное приложение (Windows .exe через PyInstaller)
============================================================================
Исправлено:
1. Сохранение CSV теперь открывает диалоговое окно Windows (выбор любой папки).
2. Разгон мотора остался сверхплавным (шаг ШИМ = 1 каждые 300 мс).
3. Добавлен счетчик шагов для прореживания точек на графике (раз в 10 шагов).
"""

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
from tkinter import filedialog  # Добавлено для вызова окна сохранения файла
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

BAUD_RATE = 115200


class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Стенд тестирования моторов")
        self.geometry("900x650")

        self.serial_port = None
        self.reader_thread = None
        self.stop_reader = threading.Event()
        self.rx_queue = queue.Queue()
        self.tx_queue = queue.Queue()
        self.is_running = False

        # Переменные для сверхплавного разгона на стороне ПК
        self.current_pwm = 1000
        self.current_pct = 0
        self.step_counter = 0  # Счетчик для прореживания графика (шаг 10)
        self.test_thread = None

        self.data = {"t": [], "pwm": [], "throttle_pct": [], "rpm": [],
                     "voltage": [], "current_a": [], "thrust": []}

        self._build_ui()
        self._refresh_ports()
        self.after(50, self._poll_queue)

    def _build_ui(self):
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

        self.label_throttle = ctk.CTkLabel(self, text="Газ: 0% (PWM: — мкс)",
                                           font=ctk.CTkFont(size=30, weight="bold"))
        self.label_throttle.pack(pady=4)

        stats = ctk.CTkFrame(self)
        stats.pack(fill="x", padx=16, pady=8)
        self.stat_labels = {}
        for i, (key, title) in enumerate([
            ("rpm", "RPM"), ("voltage", "Вольты (В)"),
            ("current_a", "Ампер (А)"), ("thrust", "Тяга")
        ]):
            f = ctk.CTkFrame(stats)
            f.grid(row=0, column=i, padx=8, pady=8, sticky="nsew")
            stats.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(f, text=title, font=ctk.CTkFont(size=11)).pack(pady=(6, 0))
            val = ctk.CTkLabel(f, text="0", font=ctk.CTkFont(size=20, weight="bold"))
            val.pack(pady=(0, 6))
            self.stat_labels[key] = val

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

        self.btn_csv = ctk.CTkButton(self, text="Сохранить CSV как...", command=self.export_csv,
                                     state="disabled")
        self.btn_csv.pack(pady=(0, 8))

        self.fig = Figure(figsize=(8, 5), dpi=100, facecolor="#1e1e1e")
        self.axs = self.fig.subplots(2, 2)
        for ax in self.axs.flat:
            ax.set_facecolor("#1e1e1e")
            ax.tick_params(colors="white", labelsize=8)
            for spine in ax.spines.values():
                spine.set_color("white")
        self.canvas = FigureCanvasTkAgg(self.fig, master=self)
        self.canvas.get_tk_widget().pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self.log_box = ctk.CTkTextbox(self, height=90, font=ctk.CTkFont(size=11))
        self.log_box.pack(fill="x", padx=16, pady=(0, 16))
        self.log_box.configure(state="disabled")

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
                    self.serial_port.write(cmd)
            except queue.Empty:
                pass
            except Exception as e:
                self.rx_queue.put(json.dumps({"__io_error__": f"запись: {e}"}))

            try:
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
        self.step_counter = 0
        
        self.label_status.configure(text="Идёт сверхплавный автоматический тест...", text_color="yellow")
        self.log("Запуск сверхплавного теста (Шаг ШИМ = 1, Пауза = 300 мс)")
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        
        self.test_thread = threading.Thread(target=self._smooth_ramp_worker, daemon=True)
        self.test_thread.start()

    def _smooth_ramp_worker(self):
        """Поток, который очень медленно и плавно шагает по 1 единице ШИМ каждые 300 мс"""
        self.tx_queue.put(b"START\n")
        
        while self.is_running and self.current_pwm < 2000:
            time.sleep(0.3)  # Сверхплавная задержка 300 мс
            if not self.is_running:
                break
                
            self.current_pwm += 1
            self.current_pct = int((self.current_pwm - 1000) / 10)
            self.step_counter += 1
            
            # Отправляем текущую точную команду ШИМ в ESP32
            cmd_str = f"PWM:{self.current_pwm}\n".encode()
            self.tx_queue.put(cmd_str)
            
        if self.is_running and self.current_pwm >= 2000:
            self.is_running = False
            self.tx_queue.put(b"STOP\n")
            self.rx_queue.put(json.dumps({"status": "DONE"}))

    def emergency_stop(self):
        self.is_running = False
        if self.serial_port:
            self.tx_queue.put(b"STOP\n")
        self.label_status.configure(text="ТЕСТ ПРИНУДИТЕЛЬНО ПРЕРВАН", text_color="red")
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

    def _handle_line(self, line):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            self.log(f"Не JSON (проигнорировано): {line}")
            return

        if "__connected__" in obj:
            self.label_status.configure(text=f"Подключено: {obj['port']}", text_color="lightgreen")
            self.btn_start.configure(state="normal")
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

            if status == "DONE":
                self.label_status.configure(text="Тест успешно завершён", text_color="lightgreen")
            elif status == "STOPPED":
                self.label_status.configure(text="Остановлено пользователем", text_color="orange")
            elif status == "SAFETY_CUTOFF":
                self.label_status.configure(text=f"Защита сработала: {reason}", text_color="red")
            
            self.is_running = False
            self.btn_start.configure(state="normal")
            self.btn_stop.configure(state="disabled")
            if self.data["t"]:
                self.btn_csv.configure(state="normal")
            return

        # Синхронизируем полученные значения с рассчитанным плавным разгоном
        obj['pwm'] = self.current_pwm
        obj['throttle_pct'] = self.current_pct

        # Добавляем данные в массивы графиков строго на каждом 10-м шаге
        if self.step_counter >= 10 or not self.is_running:
            for k in self.data:
                if k in obj:
                    self.data[k].append(obj[k])
            self.step_counter = 0
            self._update_chart()

        # Текстовые индикаторы обновляем всегда без задержек (шаг 1)
        self.label_throttle.configure(
            text=f"Газ: {self.current_pct}% (PWM: {self.current_pwm} мкс)"
        )
        for k, lbl in self.stat_labels.items():
            if k in obj:
                val = obj[k]
                if k in ("voltage", "current_a") and isinstance(val, (int, float)):
                    lbl.configure(text=f"{val:.2f}")
                else:
                    lbl.configure(text=f"{val:.0f}" if isinstance(val, (int, float)) else str(val))

    def _update_chart(self):
        x = self.data["throttle_pct"]
        plots = [
            (self.axs[0, 0], self.data["voltage"], "Напряжение (В)", "#ffb84f"),
            (self.axs[0, 1], self.data["current_a"], "Ток (А)", "#ff4d4f"),
            (self.axs[1, 0], self.data["thrust"], "Тяга", "#4fff8f"),
            (self.axs[1, 1], self.data["rpm"], "Обороты (RPM)", "#4f9dff"),
        ]
        for ax, y, title, color in plots:
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
            return
            
        # Генерация базового имени файла по умолчанию
        default_filename = f"motor_test_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        # Вызов системного окна "Сохранить как..."
        filepath = filedialog.asksaveasfilename(
            initialfile=default_filename,
            defaultextension=".csv",
            filetypes=[("CSV файлы", "*.csv"), ("Все файлы", "*.*")],
            title="Выберите папку для сохранения результатов теста"
        )
        
        # Если пользователь нажал "Отмена", то filepath будет пустым строковым значением
        if not filepath:
            self.log("Сохранение отменено пользователем")
            return
            
        try:
            keys = list(self.data.keys())
            with open(filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(keys)
                for i in range(len(self.data["t"])):
                    writer.writerow([self.data[k][i] for k in keys])
            
            # Показываем только короткое имя файла на статус-панели для красоты
            just_name = os.path.basename(filepath)
            self.label_status.configure(text=f"Сохранено: {just_name}", text_color="lightgreen")
            self.log(f"Файл успешно сохранен в: {filepath}")
        except Exception as e:
            self.log(f"Ошибка записи файла: {e}")
            self.label_status.configure(text="Ошибка при сохранении файла!", text_color="red")


if __name__ == "__main__":
    app = App()
    app.mainloop()
