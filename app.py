import eventlet

eventlet.monkey_patch()
import os
import random
import secrets
import time

import pandas as pd
from eventlet.green import threading

state_lock = threading.Lock()
bet_lock = threading.Lock()
from flask import Flask, render_template, request
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", os.urandom(24).hex())
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = (
    3600 if os.environ.get("APP_ENV") == "production" else 0
)
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_timeout=60,
    ping_interval=25,
)
ADMIN_KEY = os.environ.get("ADMIN_KEY", "dev_key_local")

players = {}
pending_disconnect = {}


def get_players_summary():
    return {
        sid: {"name": p["name"], "avatar": p["avatar"], "money": p["money"]}
        for sid, p in players.items()
    }


game_state = {
    "is_running": False,
    "round_count": 0,
    "phase": "LOBBY",
    "end_time": 0,
    "last_result": None,
    "current_quote": "Hãy sẵn sàng!",
}

event_tracker = {
    "is_bonus_active": False,
}
batched_bets = {"Bầu": 0, "Cua": 0, "Tôm": 0, "Cá": 0, "Nai": 0, "Gà": 0}
is_batching = False
is_list_updating = False


def process_batch():
    global is_batching, batched_bets
    socketio.sleep(0.4)
    with app.app_context():
        with bet_lock:
            data_to_send = batched_bets.copy()
            batched_bets = {"Bầu": 0, "Cua": 0, "Tôm": 0, "Cá": 0, "Nai": 0, "Gà": 0}
            is_batching = False
        socketio.emit("host_update_bets_batched", data_to_send, room="host_screen")
        if game_state["phase"] == "BETTING":
            socketio.emit("update_list", get_players_summary(), room="host_screen")


def process_list_update():
    global is_list_updating
    socketio.sleep(0.5)
    with app.app_context():
        socketio.emit("update_list", get_players_summary(), room="host_screen")
        is_list_updating = False


QUOTES = [
    "Phân đoạn rực rỡ nhất của một đời người không phải khoảnh khắc đạt được thành công, mà là quá trình dũng cảm và quyết tâm theo đuổi nó",
    "Học để biết, học để làm, học để chung sống, học để khẳng định mình",
    "Hành động là liều thuốc trị nỗi sợ hãi, còn sự trì hoãn sẽ nuôi dưỡng nỗi sợ hãi",
    "Vượt qua ngọn núi này, có thể phía trước vẫn là núi. Nhưng nếu không vượt qua, bạn sẽ mãi mãi chẳng biết sau ngọn núi ấy có gì",
    "Chúng ta không sinh ra để làm nền cho câu chuyện của ai, mà để làm nhân vật chính của cuộc đời mình",
    "Có thể nhỏ bé, có thể tầm thường, có thể yếu đuối — nhưng nếu bạn kiên trì, bạn vẫn có thể trở nên lớn lao và tỏa sáng",
    "Ân tùng thiện niệm khởi — Đức tự hảo tâm lai",
    "Nếu không dám bước đi vì sợ chân sẽ gãy, nhưng nếu cứ ngồi đó thì khác nào chân đã gãy",
    "Không có đơn vị nào đo lường cho sự thành công hay thất bại",
    "Nỗ lực không đảm bảo thành công, nhưng không nỗ lực đảm bảo sẽ thất bại",
    "Cứ từ từ, chẳng sao cả — cái gì làm không được một lần thì mình làm nhiều lần",
    "Tất cả những sự khó khăn thường là để chuẩn bị cho những người bình thường một số phận phi thường",
]


def load_questions_from_excel():
    try:
        current_dir = os.path.dirname(os.path.abspath(__file__))
        file_path = os.path.join(current_dir, "cau_hoi.xlsx")
        df = pd.read_excel(file_path)
        questions = []
        for _, row in df.iterrows():
            questions.append(
                {
                    "q": str(row["Câu hỏi"]).strip(),
                    "options": [
                        str(row["Lựa chọn 1"]).strip(),
                        str(row["Lựa chọn 2"]).strip(),
                        str(row["Lựa chọn 3"]).strip(),
                        str(row["Lựa chọn 4"]).strip(),
                    ],
                    "a": int(row["Đáp án"]),
                }
            )
        print(f"✅ Đã nạp thành công {len(questions)} câu hỏi từ {file_path}!")
        return questions
    except Exception as e:
        print(f"❌ Không thể đọc file Excel. Lỗi: {e}")
        return []


QUESTIONS_DB = load_questions_from_excel()


# CÔNG CỤ HỖ TRỢ
def smart_sleep(seconds):
    end_time = time.time() + seconds
    while time.time() < end_time:
        socketio.sleep(0.2)
        if not game_state["is_running"]:
            return False
        if len(players) == 0:
            print("⚠️ Hết người chơi -> Dừng game.")
            game_state["is_running"] = False
            socketio.emit("force_reload", broadcast=True)
            return False
    return True


def generate_event_schedule(total_rounds):
    if total_rounds <= 10:
        real_cnt, fake_cnt = 1, 1
    elif total_rounds <= 20:
        real_cnt, fake_cnt = 2, 3
    else:
        real_cnt, fake_cnt = 3, 4

    event_count = real_cnt + fake_cnt
    available_rounds = list(range(3, total_rounds + 1))

    # Kiểm tra số event tối đa có thể xếp mà không liên tiếp
    max_possible = (len(available_rounds) + 1) // 2

    if event_count > max_possible:
        return {}

    while True:
        positions = sorted(random.sample(available_rounds, event_count))

        if all(positions[i + 1] - positions[i] > 1 for i in range(len(positions) - 1)):
            break

    events = ["REAL"] * real_cnt + ["FAKE"] * fake_cnt
    random.shuffle(events)
    random.shuffle(positions)

    return dict(zip(positions, events))


def generate_jackpot_schedule(total_rounds, event_schedule):
    if total_rounds <= 10:
        jackpot_count = 2
    elif total_rounds <= 20:
        jackpot_count = 3
    else:
        jackpot_count = 5

    # Không chọn round đã có REAL / FAKE
    available_rounds = [
        r for r in range(3, total_rounds + 1) if r not in event_schedule
    ]

    # Tìm đủ vị trí jackpot, không đứng cạnh nhau
    while True:
        positions = sorted(random.sample(available_rounds, jackpot_count))

        if all(positions[i + 1] - positions[i] > 1 for i in range(len(positions) - 1)):
            break

    return positions


# 🌐 ROUTES
@app.route("/")
def join():
    return render_template("join.html")


@app.route("/host")
def host():
    if request.args.get("key") != ADMIN_KEY:
        return "Khu vực dành riêng cho Nhà Cái! Đi chỗ khác chơi 😎", 403
    return render_template("host.html")


@app.route("/reset")
def reset_server():
    if request.args.get("key") != ADMIN_KEY:
        return "Lêu lêu! Sai key rồi, tính phá sòng à? 😜", 403
    game_state["is_running"] = False
    game_state["phase"] = "LOBBY"
    game_state["round_count"] = 0
    players.clear()
    for timer in pending_disconnect.values():
        timer.cancel()
    pending_disconnect.clear()
    socketio.emit("force_reload", broadcast=True)
    return "Đã RESET server thành công!"


# 🎮 SOCKET EVENTS
@socketio.on("host_join")
def on_host_join():
    join_room("host_screen")
    print(" 📺  Màn hình Host đã kết nối và vào phòng VIP!")

    if game_state["is_running"]:
        current_label = "SẴN SÀNG"
        if game_state["phase"] == "QUIZ":
            current_label = "ĐANG TRẢ LỜI CÂU HỎI"
        elif game_state["phase"] == "BETTING":
            current_label = "ĐANG ĐẶT CƯỢC"
        elif game_state["phase"] == "ROLLING":
            current_label = (
                "ĐANG LẮC... (Bonus +5%)"
                if event_tracker.get("is_bonus_active")
                else "ĐANG LẮC..."
            )
        elif game_state["phase"] == "RESULT":
            current_label = (
                "KẾT QUẢ (Đã +5%)"
                if event_tracker.get("is_bonus_active")
                else "KẾT QUẢ"
            )

        emit(
            "switch_phase",
            {
                "phase": game_state["phase"],
                "label": current_label,
                "end_time": game_state["end_time"],
                "round": game_state["round_count"],
                "total_questions": len(QUESTIONS_DB),
            },
        )
        emit("update_list", get_players_summary())
        if "current_quote" in game_state:
            emit("show_quote", {"text": game_state["current_quote"]})


@socketio.on("join_game")
def on_join(data):
    sid = request.sid

    if game_state["is_running"]:
        return emit("error_msg", "Game đang diễn ra!", room=sid)

    name = data.get("name", "").strip()
    if not name:
        return emit("error_msg", "Vui lòng nhập tên!", room=sid)

    # Không cho phép 2 người dùng cùng một tên.
    # Trước đây code xóa player cũ nếu tên trùng,
    # điều này có thể làm mất state của người đang chơi.
    for p in players.values():
        if p["name"].lower() == name.lower():
            return emit("error_msg", "Tên này đã có người sử dụng!", room=sid)

    # Token bí mật dùng riêng cho reconnect.
    reconnect_token = secrets.token_urlsafe(32)

    players[sid] = {
        "name": name,
        "gender": data.get("gender", ""),
        "avatar": data.get("avatar", ""),
        "money": 200_000,
        "current_bet": {"Bầu": 0, "Cua": 0, "Tôm": 0, "Cá": 0, "Nai": 0, "Gà": 0},
        "current_bet_sum": 0,
        "used_questions": [],
        "has_answered": False,
        "win_streak": 0,
        "last_bet_time": 0,
        # Không dùng tên để xác thực reconnect nữa.
        "reconnect_token": reconnect_token,
    }

    emit("join_success", players[sid], room=sid)
    emit("update_list", get_players_summary(), broadcast=True)


@socketio.on("ping_server")
def handle_ping(client_t1):
    emit("pong_server", {"server_time": time.time(), "t1": client_t1}, room=request.sid)


@socketio.on("disconnect")
def on_disconnect():
    sid = request.sid
    if sid in players:
        print(f"⚠️ Player {players[sid]['name']} rớt mạng. Chờ 15s...")

        def delayed_remove():
            with app.app_context():
                pending_disconnect.pop(sid, None)
                if sid in players:
                    print(f"👋 Xóa vĩnh viễn {players[sid]['name']} do rớt quá lâu.")
                    del players[sid]
                    socketio.emit(
                        "update_list", get_players_summary(), room="host_screen"
                    )

        if sid in pending_disconnect:
            pending_disconnect[sid].cancel()
        pending_disconnect[sid] = eventlet.spawn_after(15.0, delayed_remove)


@socketio.on("auto_reconnect")
def auto_reconnect(data):
    sid = request.sid
    reconnect_token = data.get("reconnect_token", "").strip()

    if not reconnect_token:
        return emit("reconnect_failed", room=sid)
    found_old_sid = None

    for old_sid, p in players.items():
        if secrets.compare_digest(p.get("reconnect_token", ""), reconnect_token):
            found_old_sid = old_sid
            break

    if found_old_sid is None:
        return emit("reconnect_failed", room=sid)

    old_sid = found_old_sid
    if old_sid in pending_disconnect:
        pending_disconnect[old_sid].cancel()
        del pending_disconnect[old_sid]
    player = players.pop(old_sid)
    player["reconnect_token"] = secrets.token_urlsafe(32)

    players[sid] = player

    emit("join_success", players[sid], room=sid)

    print(f"🔄 {player['name']} đã nối lại mạng bằng reconnect token!")

    if game_state["is_running"]:
        emit(
            "switch_phase",
            {
                "phase": game_state["phase"],
                "label": "ĐANG KHÔI PHỤC...",
                "end_time": game_state["end_time"],
                "duration": 15,
            },
            room=sid,
        )

        if game_state["phase"] == "QUIZ" and "current_question" in players[sid]:
            q = players[sid]["current_question"]

            emit(
                "player_new_question", {"q": q["q"], "options": q["options"]}, room=sid
            )
            return


# 🔄 GAME LOOP LOGIC
def game_loop_thread():
    with app.app_context():
        print("🚀 Game Loop bắt đầu chạy...")

        for i in range(3, 0, -1):
            socketio.emit("countdown_start", {"count": i})
            socketio.sleep(1)
        socketio.emit("countdown_start", {"count": 0})
        socketio.sleep(0.3)

        event_tracker["is_bonus_active"] = False

        while game_state["is_running"]:
            if len(players) == 0:
                game_state["is_running"] = False
                break

            if game_state["round_count"] >= len(QUESTIONS_DB):
                print("🏁 Sắp hết game, hiện màn hình chờ...")
                socketio.emit("pre_game_over")
                socketio.sleep(4)
                socketio.emit(
                    "game_over",
                    {"msg": "ĐÃ HẾT CÂU HỎI!", "final_list": get_players_summary()},
                )
                game_state["is_running"] = False
                break

            game_state["round_count"] += 1
            event_tracker["is_bonus_active"] = False
            print(f"--- Round {game_state['round_count']} ---")

            # === PHASE 1: CHUẨN BỊ (1s) ===
            for sid, p in list(players.items()):
                avail = [
                    i for i in range(len(QUESTIONS_DB)) if i not in p["used_questions"]
                ]
                if not avail:
                    socketio.emit(
                        "game_over",
                        {"msg": "ĐÃ HẾT CÂU HỎI!", "final_list": get_players_summary()},
                    )
                    game_state["is_running"] = False
                    return

                q_idx = random.choice(avail)
                p["used_questions"].append(q_idx)
                p["current_question"] = QUESTIONS_DB[q_idx]
                p["has_answered"] = False
                socketio.emit(
                    "player_new_question",
                    {
                        "q": QUESTIONS_DB[q_idx]["q"],
                        "options": QUESTIONS_DB[q_idx]["options"],
                    },
                    room=sid,
                )

            if not smart_sleep(1):
                break

            # === PHASE 2: QUIZ (15s) ===
            QUIZ_DURATION = 15
            game_state["phase"] = "QUIZ"
            game_state["end_time"] = time.time() + QUIZ_DURATION
            game_state["start_time"] = time.time()
            game_state["current_quote"] = random.choice(QUOTES)
            socketio.emit("show_quote", {"text": game_state["current_quote"]})
            socketio.emit(
                "switch_phase",
                {
                    "phase": "QUIZ",
                    "label": "ĐANG TRẢ LỜI CÂU HỎI",
                    "round": game_state["round_count"],
                    "total_questions": len(QUESTIONS_DB),
                    "end_time": game_state["end_time"],
                    "duration": QUIZ_DURATION,
                },
            )
            if not smart_sleep(QUIZ_DURATION):
                break

            # === PHASE 3: BETTING (8s) ===
            for p in list(players.values()):
                p["current_bet"] = {
                    "Bầu": 0,
                    "Cua": 0,
                    "Tôm": 0,
                    "Cá": 0,
                    "Nai": 0,
                    "Gà": 0,
                }
                p["current_bet_sum"] = 0

            BETTING_DURATION = 8
            game_state["phase"] = "BETTING"
            game_state["end_time"] = time.time() + BETTING_DURATION
            socketio.emit(
                "switch_phase",
                {
                    "phase": "BETTING",
                    "label": "ĐANG ĐẶT CƯỢC",
                    "end_time": game_state["end_time"],
                    "duration": BETTING_DURATION,
                },
            )
            if not smart_sleep(BETTING_DURATION):
                break

            # 🛑 BỨC TƯỜNG THÉP: Khóa sổ ngay lập tức!
            game_state["phase"] = "EVENT_PROCESSING"

            # 🚨 LOGIC SỰ KIỆN
            current_round = game_state["round_count"]
            event_type = game_state.get("event_schedule", {}).get(current_round, "NONE")
            if event_type == "FAKE":
                print("🤡 Báo động giả!")
                event_tracker["is_bonus_active"] = True
                socketio.emit("raid_event", {"type": "FAKE"})
                if not smart_sleep(4):
                    break
            elif event_type == "REAL":
                print("🚨 CÔNG AN HỐT SÒNG THẬT!")

                for sid, p in list(players.items()):
                    socketio.sleep(0)
                    if random.random() <= 0.20:
                        p["money"] += p["current_bet_sum"]
                        socketio.emit(
                            "raid_result",
                            {
                                "status": "SURVIVED",
                                "msg": "Mẹ gọi về ăn cơm!\nThoát nạn, được hoàn tiền",
                            },
                            room=sid,
                        )
                        socketio.emit(
                            "update_balance", {"new_balance": p["money"]}, room=sid
                        )
                    else:
                        socketio.emit(
                            "raid_result",
                            {
                                "status": "BUSTED",
                                "msg": "TOANG RỒI!\nBị tịch thu toàn bộ tiền cược",
                            },
                            room=sid,
                        )

                    p["current_bet"] = {
                        "Bầu": 0,
                        "Cua": 0,
                        "Tôm": 0,
                        "Cá": 0,
                        "Nai": 0,
                        "Gà": 0,
                    }
                    p["current_bet_sum"] = 0

                socketio.emit("raid_event", {"type": "REAL"})
                socketio.emit("update_list", get_players_summary())
                if not smart_sleep(4):
                    break
                continue

            # === PHASE 4: LẮC XÍ NGẦU (4s) ===
            ROLLING_DURATION = 4
            game_state["phase"] = "ROLLING"
            game_state["end_time"] = time.time() + ROLLING_DURATION
            game_state["lixi_left"] = 15
            game_state["lixi_winners"] = []

            dices = ["Bầu", "Cua", "Tôm", "Cá", "Nai", "Gà"]

            if current_round in game_state.get("jackpot_schedule", []):
                lucky_animal = random.choice(dices)
                result = [lucky_animal, lucky_animal, lucky_animal]
                jackpot = True
            else:
                result = [random.choice(dices) for _ in range(3)]
                jackpot = False

            game_state["last_result"] = {
                "result": result,
                "is_jackpot": jackpot,
                "is_bonus_active": event_tracker["is_bonus_active"],
            }

            label_text = "ĐANG LẮC..."
            if event_tracker["is_bonus_active"]:
                label_text = "ĐANG LẮC... (Bonus +5%)"

            socketio.emit(
                "switch_phase",
                {
                    "phase": "ROLLING",
                    "label": label_text,
                    "end_time": game_state["end_time"],
                    "duration": ROLLING_DURATION,
                },
            )
            socketio.emit("start_shaking", {"duration": 4000})
            if not smart_sleep(ROLLING_DURATION):
                break

            # === PHASE 5: KẾT QUẢ & TRẢ THƯỞNG (6s) ===
            game_state["phase"] = "RESULT"
            bet_snapshot = {
                sid: dict(p["current_bet"]) for sid, p in list(players.items())
            }

            for sid, p in list(players.items()):
                socketio.sleep(0)
                win = 0
                for animal, bet in bet_snapshot.get(sid, {}).items():
                    if bet > 0:
                        count = result.count(animal)
                        if count > 0:
                            base_win = bet + bet * count
                            if event_tracker["is_bonus_active"]:
                                base_win = int(base_win * 1.05)
                            win += base_win

                p["money"] += win
                socketio.emit("update_balance", {"new_balance": p["money"]}, room=sid)

                # --- LOGIC XÉT CHUỖI THẮNG ---
                total_bet_amount = sum(bet_snapshot.get(sid, {}).values())
                if total_bet_amount > 0:
                    if win > 0:
                        p["win_streak"] += 1
                        if p["win_streak"] == 3:
                            lucky_bonus = random.choice([68686, 88888, 79797, 99999])
                            p["money"] += lucky_bonus
                            p["win_streak"] = 0
                            socketio.emit(
                                "streak_reward", {"bonus": lucky_bonus}, room=sid
                            )
                            socketio.emit(
                                "update_balance", {"new_balance": p["money"]}, room=sid
                            )
                    else:
                        p["win_streak"] = 0

            socketio.emit("dice_result", game_state["last_result"])
            socketio.emit("update_list", get_players_summary(), room="host_screen")

            result_label = "KẾT QUẢ"
            if event_tracker["is_bonus_active"]:
                result_label = "KẾT QUẢ (Đã +5%)"

            socketio.emit("switch_phase", {"phase": "RESULT", "label": result_label})
            if not smart_sleep(6):
                break

        print("🏁 Game Loop đã dừng hẳn.")
        game_state["is_running"] = False


# 🎮 ACTIONS
@socketio.on("start_game")
def start_game():
    if game_state["is_running"]:
        return
    if len(players) == 0:
        return emit("error_msg", "Cần ít nhất 1 người chơi!", broadcast=True)

    print("▶️ Host bắt đầu game!")
    game_state["is_running"] = True
    game_state["round_count"] = 0
    game_state["event_schedule"] = generate_event_schedule(len(QUESTIONS_DB))
    game_state["jackpot_schedule"] = generate_jackpot_schedule(
        len(QUESTIONS_DB), game_state["event_schedule"]
    )
    for p in players.values():
        p["used_questions"] = []

    emit(
        "control_music",
        {"action": "stop_lobby", "action_next": "play_game_bg"},
        broadcast=True,
    )
    socketio.start_background_task(game_loop_thread)


@socketio.on("stop_game")
def stop_game():
    print(" ⏹️  Host ấn nút DỪNG GAME KHẨN CẤP.")
    game_state["is_running"] = False
    game_state["phase"] = "LOBBY"
    game_state["round_count"] = 0
    players.clear()
    emit("force_reload", broadcast=True)


@socketio.on("submit_answer")
def submit_answer(data):
    sid = request.sid
    if sid not in players or game_state["phase"] != "QUIZ":
        return
    p = players[sid]
    if p["has_answered"]:
        return emit("error_msg", "Đã trả lời rồi!", room=sid)

    p["has_answered"] = True
    try:
        idx = int(data["answer_index"])
    except:
        return

    correct = idx == p["current_question"]["a"]
    if correct:
        time_taken = time.time() - game_state.get("start_time", time.time())
        if time_taken < 0:
            time_taken = 0
        if time_taken > 15:
            time_taken = 15

        speed_bonus = int(100000 * (1 - (time_taken / 15)))
        total_reward = 100000 + speed_bonus

        p["money"] += total_reward

        formatted_money = f"{total_reward:,}".replace(",", ".")
        msg = f"GIỎI DỊ: +{formatted_money}"
    else:
        msg = "Sai rồi kkkkkk"

    emit(
        "answer_result",
        {"correct": correct, "msg": msg, "new_balance": p["money"]},
        room=sid,
    )
    global is_list_updating
    if not is_list_updating:
        is_list_updating = True
        eventlet.spawn(process_list_update)


@socketio.on("grab_lixi")
def grab_lixi():
    sid = request.sid
    if sid not in players or game_state.get("phase") != "ROLLING":
        return
    if sid in game_state.get("lixi_winners", []):
        return

    with state_lock:
        if game_state.get("lixi_left", 0) > 0:
            game_state["lixi_left"] -= 1
            game_state["lixi_winners"].append(sid)
            success = True
        else:
            success = False

    if success:
        reward = random.choice([6868, 8888, 3979, 7979, 68686, 39790])
        players[sid]["money"] += reward
        emit(
            "lixi_success",
            {"reward": reward, "new_balance": players[sid]["money"]},
            room=sid,
        )
        socketio.emit("update_list", get_players_summary(), room="host_screen")
        if game_state["lixi_left"] == 0:
            socketio.emit("lixi_empty")


@socketio.on("place_bet")
def place_bet(data):
    sid = request.sid
    if sid not in players or game_state["phase"] != "BETTING":
        return
    p = players[sid]
    now = time.time()
    if now - p.get("last_bet_time", 0) < 0.1:
        return
    p["last_bet_time"] = now
    try:
        animal = data["animal"]
        amount = int(data["amount"])
        if amount <= 0:
            return
    except:
        return
    if animal not in ["Bầu", "Cua", "Tôm", "Cá", "Nai", "Gà"]:
        return

    if p["money"] >= amount:
        p["money"] -= amount
        p["current_bet"][animal] += amount
        p["current_bet_sum"] += amount
        emit(
            "bet_success",
            {
                "new_balance": p["money"],
                "animal": animal,
                "bet_amount": p["current_bet"][animal],
            },
            room=sid,
        )
        global is_batching, batched_bets
        with bet_lock:
            batched_bets[animal] += amount
            if not is_batching:
                is_batching = True
                eventlet.spawn(process_batch)


@socketio.on("throw_tomato")
def throw_tomato(data):
    sid = request.sid
    if sid not in players:
        return

    sorted_players = sorted(players.items(), key=lambda x: x[1]["money"], reverse=True)
    try:
        rank = int(data.get("rank", 0))
        if rank < 0 or rank >= len(sorted_players):
            return

        target_sid = sorted_players[rank][0]
        p = players[sid]

        if p["money"] >= 2000:
            p["money"] -= 2000
            emit("update_balance", {"new_balance": p["money"]}, room=sid)
            socketio.emit(
                "host_tomato_thrown",
                {"shooter_name": p["name"], "target_id": target_sid},
                room="host_screen",
            )
            socketio.emit("update_list", get_players_summary(), room="host_screen")
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    socketio.run(app, host="0.0.0.0", port=port, debug=False)
