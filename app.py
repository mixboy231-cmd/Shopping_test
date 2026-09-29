import json
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="쇼핑 리스트", page_icon="🛒")

DATA_FILE = Path(__file__).parent / "shopping_list.json"
MAX_QUANTITY = 999


@st.cache_resource
def get_file_lock():
    """모든 세션(탭)이 공유하는 파일 잠금.

    Streamlit 세션은 같은 프로세스의 스레드로 실행되므로, 읽기-수정-저장을 이 잠금으로
    직렬화해야 탭끼리 서로의 변경을 덮어쓰거나, Windows에서 교체 중인 파일을 읽다가
    실패하는 일이 없다. 한 함수 안에서 load_items/save_items를 중첩 호출하므로 RLock을 쓴다.
    """
    return threading.RLock()


FILE_LOCK = get_file_lock()

# 마크다운/HTML/LaTeX로 해석될 수 있는 문자 — 사용자 입력은 이 문자들을 엔티티로 바꿔 글자 그대로 표시한다.
SPECIAL_CHARS = set("\\`*_{}[]()#+-.!|~<>&\"'$:")


def is_valid_item(item):
    """아이템이 PRD 3장 데이터 모델(필수 키와 타입)을 따르는지 확인한다."""
    return (
        isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and bool(item["id"])
        and isinstance(item.get("name"), str)
        and bool(item["name"].strip())
        and type(item.get("quantity")) is int
        and item["quantity"] >= 1
        and isinstance(item.get("checked"), bool)
        and isinstance(item.get("created_at"), str)
    )


def backup_data_file():
    """손상된 데이터 파일을 덮어쓰기 전에 옆으로 옮겨 두고, 옮긴 파일 이름을 반환한다."""
    backup = DATA_FILE.with_name(
        f"shopping_list.corrupt-{datetime.now():%Y%m%d-%H%M%S}.json"
    )
    try:
        DATA_FILE.replace(backup)
    except OSError:
        return None
    return backup.name


def read_data_file():
    """데이터 파일의 원문을 읽는다. 파일이 없으면 None.

    다른 프로그램(편집기, 백신 등)이 잠깐 파일을 잡고 있으면 읽기가 실패할 수 있으므로
    몇 번 다시 시도한다. 그래도 실패하면 예외를 그대로 올린다 — 읽기 실패를 "손상"으로
    처리하면 멀쩡한 파일을 백업으로 옮기고 빈 목록으로 덮어쓰게 되기 때문이다.
    """
    for attempt in range(5):
        try:
            return DATA_FILE.read_bytes()
        except FileNotFoundError:
            return None
        except OSError:
            if attempt == 4:
                raise
            time.sleep(0.1)


def load_items():
    """JSON 파일에서 아이템 목록을 읽어 반환한다.

    파일이 없으면 빈 리스트. 파일 내용이 손상됐거나 잘못된 항목이 있으면 원본을 백업하고
    읽을 수 있는 항목만 남기며, 경고 문구를 session_state["load_warning"]에 남긴다.
    """
    with FILE_LOCK:
        return _load_items_locked()


def _load_items_locked():
    raw = read_data_file()
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        data = None
    if not isinstance(data, list):
        backup = backup_data_file()
        st.session_state["load_warning"] = (
            "저장된 쇼핑 리스트를 읽을 수 없어 빈 목록으로 시작합니다."
            + (f" (원본은 {backup} 으로 보관)" if backup else "")
        )
        return []

    items, seen_ids = [], set()
    for item in data:
        # id가 중복되면 위젯 key가 겹쳐 앱이 멈추므로 중복 항목도 잘못된 항목으로 본다.
        if is_valid_item(item) and item["id"] not in seen_ids:
            items.append(item)
            seen_ids.add(item["id"])
    if len(items) < len(data):
        backup = backup_data_file()
        save_items(items)
        st.session_state["load_warning"] = (
            f"저장된 쇼핑 리스트에서 읽을 수 없는 항목 {len(data) - len(items)}개를 제외했습니다."
            + (f" (원본은 {backup} 으로 보관)" if backup else "")
        )
    return items


def save_items(items):
    """아이템 목록을 JSON 파일에 저장한다.

    임시 파일에 쓴 뒤 교체하므로 저장 도중 중단돼도 기존 파일이 깨지지 않는다.
    """
    tmp = DATA_FILE.with_name(DATA_FILE.name + ".tmp")
    with FILE_LOCK:
        try:
            tmp.write_text(
                json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(DATA_FILE)
        except OSError as e:
            st.session_state["save_error"] = f"저장에 실패했습니다: {e}"


# 아래 변경 함수들은 잠금 안에서 파일을 새로 읽고, 변경을 적용해 저장한다.
# 세션(탭)마다 들고 있는 목록으로 파일을 덮어쓰면 다른 탭의 변경이 사라지기 때문이다.


def add_item(name, quantity):
    """새 아이템(id, name, quantity, checked=False, created_at)을 목록에 추가한다."""
    with FILE_LOCK:
        items = load_items()
        items.append(
            {
                "id": str(uuid.uuid4()),
                "name": name,
                "quantity": int(quantity),
                "checked": False,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        save_items(items)


def update_item(item_id, name, quantity):
    """id에 해당하는 아이템의 이름과 수량을 변경한다."""
    with FILE_LOCK:
        items = load_items()
        for item in items:
            if item["id"] == item_id:
                item["name"] = name
                item["quantity"] = int(quantity)
                break
        save_items(items)


def delete_item(item_id):
    """id에 해당하는 아이템을 목록에서 삭제한다."""
    with FILE_LOCK:
        save_items([item for item in load_items() if item["id"] != item_id])


def toggle_item(item_id, checked):
    """id에 해당하는 아이템의 체크(구매 완료) 상태를 checked로 설정한다.

    현재 상태를 반전하지 않고 체크박스의 실제 값을 그대로 저장한다.
    빠른 연속 클릭으로 콜백이 겹쳐도 화면과 저장 상태가 어긋나지 않는다.
    """
    with FILE_LOCK:
        items = load_items()
        for item in items:
            if item["id"] == item_id:
                item["checked"] = checked
                break
        save_items(items)


def escape_text(text):
    """사용자 입력을 st.markdown에서 글자 그대로 보이도록 변환한다."""
    return "".join(f"&#{ord(c)};" if c in SPECIAL_CHARS else c for c in text)

# ---- 위젯 콜백 (on_click / on_change) ----
# 콜백은 스크립트가 다시 실행되기 전에 처리되므로, 한 번의 클릭이 한 번만 반영되고
# 변경된 상태가 같은 실행에서 바로 화면에 나타난다.


QUANTITY_ERROR = f"수량은 1~{MAX_QUANTITY} 사이의 정수로 입력해 주세요."


def parse_quantity(text):
    """수량 입력(문자열)을 정수로 바꾼다. 1~MAX_QUANTITY 범위의 정수가 아니면 None.

    수량 칸은 st.number_input 대신 text_input을 쓴다. 폼 안의 number_input은 숫자를 입력하고
    바로 Enter를 누르면 직전 값이 제출되고, min/max를 걸면 범위 밖 값이 안내 없이 무시되기 때문이다.
    """
    text = text.strip()
    if not (text.isascii() and text.isdigit()):
        return None
    quantity = int(text)
    return quantity if 1 <= quantity <= MAX_QUANTITY else None


def on_add():
    name = st.session_state["new_name"].strip()
    quantity = parse_quantity(st.session_state["new_quantity"])
    if not name:
        st.session_state["add_error"] = "아이템 이름을 입력해 주세요."
        return
    if quantity is None:
        st.session_state["add_error"] = QUANTITY_ERROR
        return
    # 더블클릭으로 같은 내용이 곧바로 한 번 더 제출되면 무시한다
    # (입력창은 서버가 비우므로, 그 전에 도착한 두 번째 제출에는 같은 값이 남아 있다).
    last = st.session_state.get("last_add")
    if last and last[:2] == (name, quantity) and time.monotonic() - last[2] < 1.0:
        return
    st.session_state["last_add"] = (name, quantity, time.monotonic())
    add_item(name, quantity)
    # 추가에 성공했을 때만 입력창을 비운다 (실패 시에는 입력한 이름을 그대로 둔다).
    st.session_state["new_name"] = ""
    st.session_state["new_quantity"] = "1"


def on_toggle(item_id):
    toggle_item(item_id, st.session_state[f"check_{item_id}"])


def on_start_edit(item_id):
    item = next((it for it in load_items() if it["id"] == item_id), None)
    if item is None:
        return
    st.session_state["editing_id"] = item_id
    st.session_state["edit_error"] = None
    # 편집창을 현재 값으로 초기화 (이전에 편집하다 취소한 값이 남지 않도록)
    st.session_state[f"edit_name_{item_id}"] = item["name"]
    st.session_state[f"edit_quantity_{item_id}"] = str(item["quantity"])


def on_save_edit(item_id):
    name = st.session_state[f"edit_name_{item_id}"].strip()
    if not name:
        st.session_state["edit_error"] = "아이템 이름은 비워 둘 수 없습니다."
        return
    quantity = parse_quantity(st.session_state[f"edit_quantity_{item_id}"])
    if quantity is None:
        st.session_state["edit_error"] = QUANTITY_ERROR
        return
    update_item(item_id, name, quantity)
    st.session_state["editing_id"] = None


def on_cancel_edit():
    st.session_state["editing_id"] = None
    st.session_state["edit_error"] = None


def on_delete(item_id):
    if st.session_state["editing_id"] == item_id:
        on_cancel_edit()
    delete_item(item_id)


def on_clear_checked():
    with FILE_LOCK:
        remaining = [item for item in load_items() if not item["checked"]]
        save_items(remaining)
    if st.session_state["editing_id"] not in {item["id"] for item in remaining}:
        on_cancel_edit()


# ---- 스타일 ----
# 색은 하드코딩하지 않고 글자색(currentColor) 기반 투명도와 반투명 회색만 써서 라이트/다크 테마 모두 대응한다.
CSS = """
<style>
/* 한글은 단어 중간에서 끊지 않고, 공백 없는 긴 문자열만 어디서든 줄바꿈 */
.item-name { font-size: 1.05rem; word-break: keep-all; overflow-wrap: anywhere; }
/* 완료 항목은 흐리게 하되 배경 대비 4.5:1 이상 유지 (취소선으로도 구분됨) */
.item-name.done { text-decoration: line-through; opacity: 0.7; }
.qty-badge {
    display: inline-block;
    padding: 0.1rem 0.55rem;
    border-radius: 999px;
    font-size: 0.8rem;
    font-weight: 600;
    background: rgba(128, 128, 128, 0.18);
    white-space: nowrap;
}
.qty-badge.done { opacity: 0.7; }
.subtitle { opacity: 0.8; margin-top: -0.8rem; }
/* 좁은 화면에서도 아이템 행의 열이 세로로 쌓이지 않고 한 줄을 유지하도록 */
[class*="st-key-row_"] [data-testid="stHorizontalBlock"] { flex-wrap: nowrap; gap: 0.25rem; }
[class*="st-key-row_"] [data-testid="stColumn"] { min-width: 0 !important; }
/* 체크박스·수정·삭제 열은 터치하기 쉬운 44px 고정 폭, 이름 열이 나머지를 차지 */
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(1),
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(4),
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(5) { flex: 0 0 44px !important; }
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(2) { flex: 1 1 auto !important; }
[class*="st-key-row_"] [data-testid="stColumn"]:nth-child(3) { flex: 0 0 auto !important; }
[class*="st-key-row_"] button { min-width: 44px; min-height: 44px; }
[class*="st-key-row_"] [data-testid="stCheckbox"] label {
    min-width: 44px; min-height: 44px; justify-content: center; align-items: center;
}
/* 수정·삭제 버튼은 아이콘만 보이고, 글자("수정"/"삭제")는 스크린 리더용으로만 남긴다 */
[class*="st-key-row_"] button [data-testid="stMarkdownContainer"] {
    position: absolute; width: 1px; height: 1px; overflow: hidden;
    clip: rect(0 0 0 0); white-space: nowrap;
}
</style>
"""
st.markdown(CSS, unsafe_allow_html=True)

st.title("🛒 쇼핑 리스트")
st.markdown(
    '<p class="subtitle">사야 할 것을 적고 하나씩 체크하세요</p>',
    unsafe_allow_html=True,
)

# ---- 상태 초기화 ----
# 목록은 세션에 캐시하지 않고 매 실행마다 파일에서 읽는다 (다른 탭의 변경도 반영).
# 제목 아래에서 불러와야 손상 파일 경고가 제목 밑에 표시된다.
items = load_items()
st.session_state.setdefault("editing_id", None)
st.session_state.setdefault("edit_error", None)
st.session_state.setdefault("new_quantity", "1")

for message_key in ("load_warning", "save_error"):
    if st.session_state.get(message_key):
        st.warning(st.session_state.pop(message_key))

# ---- F1. 추가 ----
with st.form("add_form", border=True):
    col_name, col_qty, col_btn = st.columns([5, 1.6, 1.2], vertical_alignment="bottom")
    col_name.text_input("아이템 이름", key="new_name", placeholder="예: 우유")
    col_qty.text_input("수량", key="new_quantity")
    col_btn.form_submit_button("추가", on_click=on_add, type="primary", width="stretch")

if st.session_state.get("add_error"):
    st.warning(st.session_state.pop("add_error"))

# ---- F5. 요약 ----
done_count = sum(item["checked"] for item in items)
col_bar, col_text = st.columns([3, 1.3], vertical_alignment="center")
col_bar.progress(done_count / len(items) if items else 0.0)
col_text.markdown(f"**전체 {len(items)}개 / 완료 {done_count}개**")

# ---- F2~F4. 목록 ----
if not items:
    st.info("쇼핑 리스트가 비어 있습니다", icon="📝")

# 추가한 순서대로 표시한다. 체크할 때마다 행 순서가 바뀌면(정렬) 빠른 연속 클릭 시
# Streamlit 화면에 이전 행이 남는 "유령 행" 문제가 생기므로 정렬하지 않는다.
for item in items:
    item_id = item["id"]

    editing = st.session_state["editing_id"] == item_id
    # 편집 행은 좁은 화면에서 세로로 쌓이도록 한 줄 유지 CSS(st-key-row_)에서 제외한다.
    row_key = f"editrow_{item_id}" if editing else f"row_{item_id}"
    with st.container(border=True, key=row_key):
        if editing:
            # 폼으로 감싸 이름·수량 칸에서 Enter를 누르면 첫 번째 제출 버튼(저장)이 눌린다.
            with st.form(f"edit_form_{item_id}", border=False):
                col_name, col_qty, col_save, col_cancel = st.columns(
                    [5, 1.8, 1, 1], vertical_alignment="bottom"
                )
                col_name.text_input("이름 수정", key=f"edit_name_{item_id}")
                col_qty.text_input("수량 수정", key=f"edit_quantity_{item_id}")
                col_save.form_submit_button(
                    "저장",
                    key=f"save_{item_id}",
                    on_click=on_save_edit,
                    args=(item_id,),
                    type="primary",
                    width="stretch",
                )
                col_cancel.form_submit_button(
                    "취소",
                    key=f"cancel_{item_id}",
                    on_click=on_cancel_edit,
                    width="stretch",
                )
            if st.session_state["edit_error"]:
                st.warning(st.session_state["edit_error"])
            continue

        col_check, col_name, col_qty, col_edit, col_del = st.columns(
            [0.5, 5, 1.5, 0.7, 0.7], vertical_alignment="center"
        )
        # 체크박스 값은 항상 파일 데이터에 맞춘다 (다른 탭에서 바뀐 경우 포함).
        st.session_state[f"check_{item_id}"] = item["checked"]
        # 라벨은 화면에서 숨기지만 스크린 리더가 어떤 아이템의 체크박스인지 읽을 수 있게 이름을 넣는다.
        # 라벨 원문이 그대로 aria-label이 되므로 이스케이프하지 않는다 (라벨은 HTML을 해석하지 않음).
        col_check.checkbox(
            f"{item['name']} 구매 완료",
            key=f"check_{item_id}",
            on_change=on_toggle,
            args=(item_id,),
            label_visibility="collapsed",
        )
        done_class = " done" if item["checked"] else ""
        col_name.markdown(
            f'<span class="item-name{done_class}">{escape_text(item["name"])}</span>',
            unsafe_allow_html=True,
        )
        col_qty.markdown(
            f'<span class="qty-badge{done_class}">x{item["quantity"]}</span>',
            unsafe_allow_html=True,
        )
        col_edit.button(
            "수정",
            icon=":material/edit:",
            key=f"edit_{item_id}",
            on_click=on_start_edit,
            args=(item_id,),
            help="수정",
            type="tertiary",
        )
        col_del.button(
            "삭제",
            icon=":material/delete:",
            key=f"delete_{item_id}",
            on_click=on_delete,
            args=(item_id,),
            help="삭제",
            type="tertiary",
        )

if items:
    _, col_clear = st.columns([3, 1.3])
    col_clear.button(
        "완료 항목 삭제",
        on_click=on_clear_checked,
        disabled=done_count == 0,
        width="stretch",
    )
