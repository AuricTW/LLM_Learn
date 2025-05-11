import base64
import json
import os
import queue
import socket
import threading
import time
import mysql.connector
import pyaudio
import socks
import websocket
from datetime import datetime

# 設定 SOCKS5 代理（若不需要代理可移除）
socket.socket = socks.socksocket

# OpenAI API Key
API_KEY = ""  # 請替換為您的 API Key
if not API_KEY:
    raise ValueError("缺少 API Key，請設定 'OPENAI_API_KEY' 環境變數或直接在程式中指定。")

# OpenAI WebSocket 端點
WS_URL = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-12-17'

# 音訊相關參數
CHUNK_SIZE = 1024      # 每次處理音訊塊大小（單位：bytes）
RATE = 24000           # 取樣率（Hz）
FORMAT = pyaudio.paInt16 # 音訊格式

# 播放端與麥克風端的音訊緩衝 / 佇列
audio_buffer = bytearray()   # 用來儲存 AI 回傳的音訊資料
mic_queue = queue.Queue()    # 用來儲存麥克風收音資料

# 執行緒停止事件
stop_event = threading.Event()

# 這些變數用於暫時抑制麥克風，避免 AI 的聲音又被錄進去
mic_on_at = 0
mic_active = None
REENGAGE_DELAY_MS = 500  # 在播放音訊後，多少毫秒內關閉麥克風

# 資料庫連線設定
DB_CONFIG = {
    'host': '',
    'port': '',
    'user': '',
    'password': '',
    'database': ''  # 改為醫療系統資料庫
}

# -----------------------------------------------------
# 資料庫操作函式
# -----------------------------------------------------

# 建立資料庫連線
def get_db_connection():
    try:
        connection = mysql.connector.connect(**DB_CONFIG)
        return connection
    except mysql.connector.Error as err:
        print(f"⚠️ 資料庫連線錯誤: {err}")
        return None

# 新增病人資料
def add_patient(name, age, gender):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = "INSERT INTO patients (name, age, gender) VALUES (%s, %s, %s)"
        values = (name, age, gender)
        cursor.execute(query, values)
        connection.commit()
        patient_id = cursor.lastrowid
        cursor.close()
        connection.close()
        return f"✅ 病人 {name} 資料已成功新增！病人ID：{patient_id}"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 新增病人資訊紀錄
def add_patient_record(patient_id, height, weight, diet, exercise, inconvenience, sensor_data):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = """INSERT INTO patient_records 
                 (patient_id, height, weight, diet, exercise, inconvenience, sensor_data) 
                 VALUES (%s, %s, %s, %s, %s, %s, %s)"""
        
        sensor_data_json = json.dumps(sensor_data, ensure_ascii=False)
        values = (patient_id, height, weight, diet, exercise, inconvenience, sensor_data_json)
        cursor.execute(query, values)
        connection.commit()
        cursor.close()
        connection.close()
        return f"✅ 病人 {patient_id} 的健康紀錄已成功新增！"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 查詢病人資料
def query_patient(patient_id):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = "SELECT * FROM patients WHERE id = %s"
        cursor.execute(query, (patient_id,))
        result = cursor.fetchone()
        
        if result:
            response = f"📌 病人資料：\n姓名: {result[1]}, 年齡: {result[2]}, 性別: {result[3]}"
            cursor.close()
            connection.close()
            return response
        else:
            cursor.close()
            connection.close()
            return f"❌ 找不到病人 ID: {patient_id} 的資料"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 列出所有病人
def list_patients():
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM patients")
        records = cursor.fetchall()
        
        if records:
            response = "📋 病人列表：\n"
            for r in records:
                response += f"ID: {r[0]}, 姓名: {r[1]}, 年齡: {r[2]}, 性別: {r[3]}\n"
            cursor.close()
            connection.close()
            return response
        else:
            cursor.close()
            connection.close()
            return "資料表為空"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 查詢醫生評估報告
def get_doctor_reports(patient_id):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = "SELECT * FROM doctor_reports WHERE patient_id = %s ORDER BY created_at DESC"
        cursor.execute(query, (patient_id,))
        reports = cursor.fetchall()
        
        if reports:
            response = f"📋 病人 {patient_id} 的醫生報告：\n"
            for report in reports:
                response += f"ID: {report[0]}, 回饋: {report[2]}, 評估: {report[3]}, 已審閱: {report[4]}, 筆記: {report[5]}\n"
            cursor.close()
            connection.close()
            return response
        else:
            cursor.close()
            connection.close()
            return f"❌ 找不到病人 {patient_id} 的醫生報告"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 刪除病患資料
def delete_patient(patient_id):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        # 先刪除相關的記錄
        cursor.execute("DELETE FROM patient_records WHERE patient_id = %s", (patient_id,))
        cursor.execute("DELETE FROM doctor_reports WHERE patient_id = %s", (patient_id,))
        cursor.execute("DELETE FROM conversation_messages WHERE patient_id = %s", (patient_id,))
        cursor.execute("DELETE FROM conversation_sessions WHERE patient_id = %s", (patient_id,))
        # 最後刪除病患基本資料
        cursor.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        connection.commit()
        cursor.close()
        connection.close()
        return f"✅ 已成功刪除病患 ID: {patient_id} 的所有相關資料"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 新增醫生評估報告（僅醫生可見）
def insert_doctor_report(patient_id, feedback, evaluation, reviewed=False, notes=""):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = """INSERT INTO doctor_reports 
                 (patient_id, feedback, evaluation, reviewed, notes) 
                 VALUES (%s, %s, %s, %s, %s)"""
        
        values = (patient_id, feedback, evaluation, reviewed, notes)
        cursor.execute(query, values)
        connection.commit()
        cursor.close()
        connection.close()
        return f"✅ 已成功新增醫生評估報告"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 新增意見回饋（病患可見）
def insert_feedback(patient_id, feedback):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = """INSERT INTO patient_feedback 
                 (patient_id, feedback) 
                 VALUES (%s, %s)"""
        
        values = (patient_id, feedback)
        cursor.execute(query, values)
        connection.commit()
        cursor.close()
        connection.close()
        return f"✅ 已成功新增意見回饋"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 查詢病患的意見回饋
def get_patient_feedback(patient_id):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = "SELECT * FROM patient_feedback WHERE patient_id = %s ORDER BY created_at DESC"
        cursor.execute(query, (patient_id,))
        feedbacks = cursor.fetchall()
        
        if feedbacks:
            response = f"📋 您的意見回饋：\n"
            for feedback in feedbacks:
                response += f"時間: {feedback[3]}\n內容: {feedback[2]}\n"
            cursor.close()
            connection.close()
            return response
        else:
            cursor.close()
            connection.close()
            return f"❌ 目前沒有意見回饋"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# 結束對話
def end_conversation(session_id):
    try:
        connection = get_db_connection()
        if not connection:
            return "⚠️ 無法連接到資料庫"
        
        cursor = connection.cursor()
        query = "UPDATE conversation_sessions SET end_time = CURRENT_TIMESTAMP WHERE id = %s"
        cursor.execute(query, (session_id,))
        connection.commit()
        cursor.close()
        connection.close()
        return f"✅ 對話 {session_id} 已成功結束"
    except mysql.connector.Error as err:
        return f"⚠️ 發生錯誤: {err}"

# -----------------------------------------------------
# 音訊處理函式
# -----------------------------------------------------

# 清空音訊緩衝的函式
def clear_audio_buffer():
    global audio_buffer
    audio_buffer = bytearray()
    print('🔵 音訊緩衝已清空')

# 停止播放音訊的函式
def stop_audio_playback():
    global is_playing
    is_playing = False
    print('🔵 停止音訊播放')

# 麥克風輸入回呼函式
def mic_callback(in_data, frame_count, time_info, status):
    global mic_on_at, mic_active

    # 如果麥克風目前狀態不是「已啟用」，則印出啟用訊息
    if mic_active != True:
        print('🎙️🟢 麥克風已啟用')
        mic_active = True

    # 將錄到的音訊放入 mic_queue，後續由 send_mic_audio_to_websocket 傳送給伺服器
    mic_queue.put(in_data)

    # 以 None 表示此回呼無需回傳額外音訊
    return (None, pyaudio.paContinue)

# 傳送麥克風音訊至 WebSocket 的執行緒函式
def send_mic_audio_to_websocket(ws):
    try:
        while not stop_event.is_set():
            if not mic_queue.empty():
                mic_chunk = mic_queue.get()
                # 將音訊資料編碼為 base64 字串
                encoded_chunk = base64.b64encode(mic_chunk).decode('utf-8')
                message = json.dumps({'type': 'input_audio_buffer.append', 'audio': encoded_chunk})
                try:
                    ws.send(message)
                except Exception as e:
                    print(f'傳送麥克風音訊錯誤: {e}')
    except Exception as e:
        print(f'麥克風音訊傳送執行緒發生異常: {e}')
    finally:
        print('麥克風音訊傳送執行緒結束')

# 播放端的回呼函式，將 audio_buffer 中的資料播放出來
def speaker_callback(in_data, frame_count, time_info, status):
    global audio_buffer, mic_on_at

    # 需要多少 bytes
    bytes_needed = frame_count * 2
    current_buffer_size = len(audio_buffer)

    if current_buffer_size >= bytes_needed:
        # 若緩衝夠，取出對應大小音訊
        audio_chunk = bytes(audio_buffer[:bytes_needed])
        audio_buffer = audio_buffer[bytes_needed:]
        # 播放音訊後，設定 mic_on_at，用於抑制麥克風
        mic_on_at = time.time() + REENGAGE_DELAY_MS / 1000
    else:
        # 若緩衝不夠，就補零
        audio_chunk = bytes(audio_buffer) + b'\x00' * (bytes_needed - current_buffer_size)
        audio_buffer.clear()

    return (audio_chunk, pyaudio.paContinue)

# 從 WebSocket 接收音訊並處理事件的執行緒函式
def receive_audio_from_websocket(ws):
    global audio_buffer

    try:
        while not stop_event.is_set():
            try:
                message = ws.recv()
                if not message:
                    print('🔵 收到空訊息（可能是 EOF 或 WebSocket 關閉）')
                    break

                # 將接收的字串轉為 JSON
                message = json.loads(message)
                event_type = message['type']
                print(f'⚡️ 收到 WebSocket 事件: {event_type}')

                # session.created 代表成功建立會話
                if event_type == 'session.created':
                    send_fc_session_update(ws)

                # response.audio.delta 代表 AI 端傳來新的音訊資料
                elif event_type == 'response.audio.delta':
                    audio_content = base64.b64decode(message['delta'])
                    audio_buffer.extend(audio_content)
                    print(f'🔵 收到 {len(audio_content)} 位元組，總緩衝大小: {len(audio_buffer)}')

                # input_audio_buffer.speech_started 表示伺服器偵測到使用者語音開始
                elif event_type == 'input_audio_buffer.speech_started':
                    print('🔵 語音開始，清空緩衝並停止播放')
                    clear_audio_buffer()
                    stop_audio_playback()

                # response.audio.done 代表 AI 語音播放結束
                elif event_type == 'response.audio.done':
                    print('🔵 AI 語音播放結束')

                # response.function_call_arguments.done 代表 AI 執行函式呼叫參數已傳完
                elif event_type == 'response.function_call_arguments.done':
                    handle_function_call(message, ws)

            except Exception as e:
                print(f'接收音訊錯誤: {e}')
    except Exception as e:
        print(f'接收音訊執行緒發生異常: {e}')
    finally:
        print('接收音訊執行緒結束')

# -----------------------------------------------------
# 處理 AI 呼叫函式的邏輯
# -----------------------------------------------------

def handle_function_call(event_json, ws):
    try:
        name = event_json.get("name", "")
        call_id = event_json.get("call_id", "")
        arguments = event_json.get("arguments", "{}")
        function_call_args = json.loads(arguments)

        print(f"處理函式呼叫: {name}, 參數: {function_call_args}")

        if name == "start_conversation":
            patient_id = function_call_args.get("patient_id", 0)
            result = start_conversation(patient_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "add_patient":
            patient_name = function_call_args.get("name", "")
            age = function_call_args.get("age", 0)
            gender = function_call_args.get("gender", "")
            result = add_patient(patient_name, age, gender)
            send_function_call_result(result, call_id, ws)
            
        elif name == "add_patient_record":
            patient_id = function_call_args.get("patient_id", 0)
            height = function_call_args.get("height", 0)
            weight = function_call_args.get("weight", 0)
            diet = function_call_args.get("diet", "")
            exercise = function_call_args.get("exercise", "")
            inconvenience = function_call_args.get("inconvenience", "")
            sensor_data = function_call_args.get("sensor_data", {})
            result = add_patient_record(patient_id, height, weight, diet, exercise, inconvenience, sensor_data)
            send_function_call_result(result, call_id, ws)
            
        elif name == "update_patient_record":
            patient_id = function_call_args.get("patient_id", 0)
            record_id = function_call_args.get("record_id", 0)
            height = function_call_args.get("height")
            weight = function_call_args.get("weight")
            diet = function_call_args.get("diet")
            exercise = function_call_args.get("exercise")
            inconvenience = function_call_args.get("inconvenience")
            sensor_data = function_call_args.get("sensor_data")
            result = update_patient_record(patient_id, record_id, height, weight, diet, exercise, inconvenience, sensor_data)
            send_function_call_result(result, call_id, ws)
            
        elif name == "query_patient":
            patient_id = function_call_args.get("patient_id", 0)
            result = query_patient(patient_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "list_patients":
            result = list_patients()
            send_function_call_result(result, call_id, ws)
            
        elif name == "insert_doctor_report":
            patient_id = function_call_args.get("patient_id", 0)
            feedback = function_call_args.get("feedback", "")
            evaluation = function_call_args.get("evaluation", "")
            reviewed = function_call_args.get("reviewed", False)
            notes = function_call_args.get("notes", "")
            result = insert_doctor_report(patient_id, feedback, evaluation, reviewed, notes)
            send_function_call_result(result, call_id, ws)
            
        elif name == "insert_feedback":
            patient_id = function_call_args.get("patient_id", 0)
            feedback = function_call_args.get("feedback", "")
            result = insert_feedback(patient_id, feedback)
            send_function_call_result(result, call_id, ws)
            
        elif name == "get_patient_feedback":
            patient_id = function_call_args.get("patient_id", 0)
            result = get_patient_feedback(patient_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "get_doctor_reports":
            patient_id = function_call_args.get("patient_id", 0)
            result = get_doctor_reports(patient_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "get_conversation_sessions":
            patient_id = function_call_args.get("patient_id", 0)
            result = get_conversation_sessions(patient_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "get_patient_conversation":
            patient_id = function_call_args.get("patient_id", 0)
            session_id = function_call_args.get("session_id", 0)
            result = get_patient_conversation(patient_id, session_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "end_conversation":
            session_id = function_call_args.get("session_id", 0)
            result = end_conversation(session_id)
            send_function_call_result(result, call_id, ws)
            
        elif name == "delete_patient":
            patient_id = function_call_args.get("patient_id", 0)
            result = delete_patient(patient_id)
            send_function_call_result(result, call_id, ws)
            
    except Exception as e:
        print(f"處理函式呼叫時發生錯誤: {e}")
        send_function_call_result(f"執行函式時發生錯誤: {e}", call_id, ws)

# 將函式呼叫結果回傳給 WebSocket 伺服器
def send_function_call_result(result, call_id, ws):
    result_json = {
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "output": result,
            "call_id": call_id
        }
    }
    try:
        ws.send(json.dumps(result_json))
        print(f"已傳送函式呼叫結果: {result}")

        rp_json = {"type": "response.create"}
        ws.send(json.dumps(rp_json))
    except Exception as e:
        print(f"傳送函式呼叫結果失敗: {e}")

# -----------------------------------------------------
# WebSocket 連線相關函式
# -----------------------------------------------------

# 傳送 session 配置給伺服器，如聲音、工具等
def send_fc_session_update(ws):
    session_config = {
        "type": "session.update",
        "session": {
            "instructions": (
                "你是一個醫療保健助手，首先需要確認使用者的身份（醫生或病患）。"
                "如果使用者是病患：\n"
                "1. 使用 start_conversation 開啟新的對話\n"
                "2. 像個專業醫生一樣與病患對話，了解其症狀和健康狀況\n"
                "3. 使用 add_patient 記錄病患基本資料\n"
                "4. 使用 add_patient_record 記錄病患的健康狀況\n"
                "5. 使用 update_patient_record 更新病患的資訊\n"
                "6. 在對話結束時：\n"
                "   - 使用 insert_feedback 提供病患可見的意見回饋\n"
                "   - 使用 insert_doctor_report 記錄醫生內部評估報告\n"
                "7. 使用 end_conversation 結束對話\n\n"
                "如果使用者是醫生：\n"
                "1. 提供查詢病患資料的功能\n"
                "2. 允許修改病患紀錄\n"
                "3. 可以撰寫和修改醫生評估報告\n"
                "4. 可以查看所有病患的對話記錄\n"
                "5. 可以刪除病患資料（包含所有相關記錄）\n"
                "請使用繁體中文回應用戶的問題，並且盡可能使用函式來執行資料庫操作。"
                "如果用戶的指令不明確，請詢問更多細節。"
            ),
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 300,
                "silence_duration_ms": 500
            },
            "voice": "alloy",
            "temperature": 1,
            "max_response_output_tokens": 4096,
            "modalities": ["text", "audio"],
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {
                "model": "whisper-1"
            },
            "tool_choice": "auto",
            "tools": [ 
                {
                    "type": "function",
                    "name": "start_conversation",
                    "description": "開啟新的對話",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "add_patient",
                    "description": "新增病人基本資料",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "病人姓名"
                            },
                            "age": {
                                "type": "integer",
                                "description": "病人年齡"
                            },
                            "gender": {
                                "type": "string",
                                "description": "病人性別",
                                "enum": ["男", "女"]
                            }
                        },
                        "required": ["name", "age", "gender"]
                    }
                },
                {
                    "type": "function",
                    "name": "add_patient_record",
                    "description": "新增病人健康紀錄",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            },
                            "height": {
                                "type": "number",
                                "description": "身高(公分)"
                            },
                            "weight": {
                                "type": "number",
                                "description": "體重(公斤)"
                            },
                            "diet": {
                                "type": "string",
                                "description": "飲食狀況"
                            },
                            "exercise": {
                                "type": "string",
                                "description": "運動狀況"
                            },
                            "inconvenience": {
                                "type": "string",
                                "description": "身體不適狀況"
                            },
                            "sensor_data": {
                                "type": "object",
                                "description": "感測器數據",
                                "properties": {
                                    "grip": {"type": "number"},
                                    "sit_up": {"type": "number"}
                                }
                            }
                        },
                        "required": ["patient_id", "height", "weight"]
                    }
                },
                {
                    "type": "function",
                    "name": "update_patient_record",
                    "description": "更新病人健康紀錄",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            },
                            "record_id": {
                                "type": "integer",
                                "description": "紀錄ID"
                            },
                            "height": {
                                "type": "number",
                                "description": "身高(公分)"
                            },
                            "weight": {
                                "type": "number",
                                "description": "體重(公斤)"
                            },
                            "diet": {
                                "type": "string",
                                "description": "飲食狀況"
                            },
                            "exercise": {
                                "type": "string",
                                "description": "運動狀況"
                            },
                            "inconvenience": {
                                "type": "string",
                                "description": "身體不適狀況"
                            },
                            "sensor_data": {
                                "type": "object",
                                "description": "感測器數據"
                            }
                        },
                        "required": ["patient_id", "record_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "query_patient",
                    "description": "查詢病人資料",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "list_patients",
                    "description": "列出所有病人資料",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                },
                {
                    "type": "function",
                    "name": "insert_doctor_report",
                    "description": "新增醫生評估報告",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            },
                            "feedback": {
                                "type": "string",
                                "description": "病患回饋"
                            },
                            "evaluation": {
                                "type": "string",
                                "description": "評估報告"
                            },
                            "reviewed": {
                                "type": "boolean",
                                "description": "是否已審閱"
                            },
                            "notes": {
                                "type": "string",
                                "description": "醫生筆記"
                            }
                        },
                        "required": ["patient_id", "feedback", "evaluation"]
                    }
                },
                {
                    "type": "function",
                    "name": "insert_feedback",
                    "description": "新增病患可見的意見回饋",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            },
                            "feedback": {
                                "type": "string",
                                "description": "意見回饋內容"
                            }
                        },
                        "required": ["patient_id", "feedback"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_patient_feedback",
                    "description": "查詢病患的意見回饋",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_doctor_reports",
                    "description": "查詢醫生評估報告",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_conversation_sessions",
                    "description": "查詢病人的對話記錄",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "get_patient_conversation",
                    "description": "查看特定對話的詳細內容",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病人ID"
                            },
                            "session_id": {
                                "type": "integer",
                                "description": "對話ID"
                            }
                        },
                        "required": ["patient_id", "session_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "end_conversation",
                    "description": "結束對話",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "session_id": {
                                "type": "integer",
                                "description": "對話ID"
                            }
                        },
                        "required": ["session_id"]
                    }
                },
                {
                    "type": "function",
                    "name": "delete_patient",
                    "description": "刪除病患資料（包含所有相關記錄）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "patient_id": {
                                "type": "integer",
                                "description": "病患ID"
                            }
                        },
                        "required": ["patient_id"]
                    }
                }
            ]
        }
    }

    session_config_json = json.dumps(session_config)
    print(f"傳送 session 更新: {session_config_json}")
    try:
        ws.send(session_config_json)
    except Exception as e:
        print(f"傳送 session 更新失敗: {e}")

# 強制使用 IPv4 建立連線的函式
def create_connection_with_ipv4(*args, **kwargs):
    original_getaddrinfo = socket.getaddrinfo

    def getaddrinfo_ipv4(host, port, family=socket.AF_INET, *args):
        return original_getaddrinfo(host, port, socket.AF_INET, *args)

    socket.getaddrinfo = getaddrinfo_ipv4
    try:
        return websocket.create_connection(*args, **kwargs)
    finally:
        socket.getaddrinfo = original_getaddrinfo

# 與 OpenAI WebSocket API 建立連線，並啟動接收/傳送執行緒
def connect_to_openai():
    ws = None
    try:
        ws = create_connection_with_ipv4(
            WS_URL,
            header=[
                f'Authorization: Bearer {API_KEY}',
                'OpenAI-Beta: realtime=v1'
            ]
        )
        print('已連接到 OpenAI WebSocket')

        # 開始接收執行緒
        receive_thread = threading.Thread(target=receive_audio_from_websocket, args=(ws,))
        receive_thread.start()

        # 開始傳送麥克風音訊執行緒
        mic_thread = threading.Thread(target=send_mic_audio_to_websocket, args=(ws,))
        mic_thread.start()

        # 等待 stop_event，若 stop_event.set() 則跳出
        while not stop_event.is_set():
            time.sleep(0.1)

        print('傳送 WebSocket 關閉訊號')
        ws.send_close()

        receive_thread.join()
        mic_thread.join()

        print('WebSocket 已關閉，執行緒已終止')
    except Exception as e:
        print(f'連接到 OpenAI 失敗: {e}')
    finally:
        if ws is not None:
            try:
                ws.close()
                print('WebSocket 連接已關閉')
            except Exception as e:
                print(f'關閉 WebSocket 連接時發生錯誤: {e}')

# -----------------------------------------------------
# 主程式入口
# -----------------------------------------------------
def main():
    print("=== 醫療保健語音助手 ===")
    print("正在初始化音訊系統...")
    
    p = pyaudio.PyAudio()

    mic_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        input=True,
        stream_callback=mic_callback,
        frames_per_buffer=CHUNK_SIZE
    )

    speaker_stream = p.open(
        format=FORMAT,
        channels=1,
        rate=RATE,
        output=True,
        stream_callback=speaker_callback,
        frames_per_buffer=CHUNK_SIZE
    )

    try:
        mic_stream.start_stream()
        speaker_stream.start_stream()
        
        print("音訊系統已啟動")
        print("正在連接到 OpenAI...")
        print("連接成功後，您可以開始與語音助手對話")
        print("按 Ctrl+C 結束程式")

        connect_to_openai()

        while mic_stream.is_active() and speaker_stream.is_active():
            time.sleep(0.1)

    except KeyboardInterrupt:
        print('正在關閉程式...')
        stop_event.set()

    finally:
        mic_stream.stop_stream()
        mic_stream.close()
        speaker_stream.stop_stream()
        speaker_stream.close()

        p.terminate()
        print('音訊系統已關閉，程式結束')

if __name__ == '__main__':
    main() 