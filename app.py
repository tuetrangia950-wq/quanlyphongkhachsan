from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import os
import hmac
import hashlib
import secrets
import mysql.connector
import pandas as pd
import plotly.express as px
import streamlit as st
from google import genai

BASE = Path(__file__).resolve().parent
IMAGE = BASE / 'khachsan.jpg'
TYPES = {'Standard': 800_000, 'Superior': 1_100_000, 'Deluxe': 1_500_000,
         'Suite': 2_500_000, 'Villa': 4_500_000}
HOUSEKEEPING = ['Sạch', 'Bẩn', 'Đang vệ sinh', 'Bảo trì']

def today():
    return datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).date()

st.set_page_config(page_title='Khách sạn Hi Vọng', page_icon='🏨', layout='wide')

# Thông tin kết nối Aiven MySQL
DB_USER = "avnadmin"
DB_PASSWORD = st.secrets.get('DB_PASSWORD', os.environ.get('DB_PASSWORD', ''))
DB_HOST = "mysql-39428747-tuetrangia950-3ce0.j.aivencloud.com"
DB_PORT = 27114
DB_NAME = "defaultdb"

# Cấu hình Gemini AI Key
GEMINI_KEY = st.secrets.get("GEMINI_API_KEY", os.environ.get("GEMINI_API_KEY", ""))

DB_CONFIG = dict(
    host=DB_HOST, port=DB_PORT, user=DB_USER,
    password=DB_PASSWORD, database=DB_NAME,
    connection_timeout=15, ssl_disabled=False,
    charset="utf8mb4", use_unicode=True,
)

class Row(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)

class DBConnection:
    def __init__(self, connection):
        self.connection = connection
    def execute(self, sql, params=()):
        cur = self.connection.cursor(dictionary=True)
        cur.execute(sql.replace('?', '%s'), tuple(params))
        return Result(cur)
    def executemany(self, sql, params):
        cur = self.connection.cursor()
        try:
            cur.executemany(sql.replace('?', '%s'), params)
        finally:
            cur.close()

class Result:
    def __init__(self, cursor):
        self.cursor = cursor
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount
        if not cursor.with_rows:
            cursor.close()
    def fetchone(self):
        row = self.cursor.fetchone()
        self.cursor.close()
        return Row(row) if row is not None else None
    def fetchall(self):
        rows = self.cursor.fetchall()
        self.cursor.close()
        return [Row(row) for row in rows]

@contextmanager
def connect():
    conn = mysql.connector.connect(**DB_CONFIG)
    conn.time_zone = '+07:00'
    try:
        yield DBConnection(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def read(sql, params=()):
    with connect() as conn:
        cur = conn.connection.cursor(dictionary=True)
        try:
            cur.execute(sql.replace('?', '%s'), tuple(params))
            cols = cur.column_names
            rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        finally:
            cur.close()

def write(sql, params=()):
    with connect() as conn:
        return conn.execute(sql, params).lastrowid

def initialize():
    statements = [
        """CREATE TABLE IF NOT EXISTS rooms (
            id INT AUTO_INCREMENT PRIMARY KEY,
            number VARCHAR(30) NOT NULL UNIQUE,
            room_type VARCHAR(50) NOT NULL,
            price BIGINT NOT NULL,
            status VARCHAR(40) NOT NULL DEFAULT 'Sạch',
            note TEXT NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS customers (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            phone VARCHAR(50) NOT NULL DEFAULT '',
            email VARCHAR(255) NOT NULL DEFAULT ''
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS bookings (
            id INT AUTO_INCREMENT PRIMARY KEY,
            customer_id INT NOT NULL,
            room_id INT NOT NULL,
            checkin VARCHAR(10) NOT NULL,
            checkout VARCHAR(10) NOT NULL,
            actual_checkout VARCHAR(10) NULL,
            status VARCHAR(40) NOT NULL DEFAULT 'Đã đặt',
            total BIGINT NOT NULL DEFAULT 0,
            note TEXT NOT NULL,
            INDEX idx_room_dates (room_id, checkin, checkout),
            CONSTRAINT fk_customer FOREIGN KEY (customer_id) REFERENCES customers(id),
            CONSTRAINT fk_room FOREIGN KEY (room_id) REFERENCES rooms(id),
            CONSTRAINT chk_dates CHECK (checkout > checkin)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
        """CREATE TABLE IF NOT EXISTS employees (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(80) NOT NULL UNIQUE,
            full_name VARCHAR(255) NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(20) NOT NULL DEFAULT 'staff',
            active TINYINT(1) NOT NULL DEFAULT 1
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    ]
    with connect() as conn:
        for sql in statements:
            conn.execute(sql)
        
        if conn.execute('SELECT COUNT(*) FROM rooms').fetchone()[0] == 0:
            conn.executemany('INSERT INTO rooms(number,room_type,price,status,note) VALUES(?,?,?,?,?)',
                [(n, t, p, 'Sạch', '') for n,t,p in [
                    ('101','Standard',800000),('102','Standard',800000),
                    ('201','Superior',1100000),('202','Superior',1100000),
                    ('301','Deluxe',1500000),('302','Deluxe',1500000),
                    ('401','Suite',2500000),('501','Villa',4500000)]])

        for field in ('adults', 'children'):
            found = conn.execute(
                'SELECT COUNT(*) FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=?',
                ('bookings', field)
            ).fetchone()[0]
            if not found:
                conn.execute(f'ALTER TABLE bookings ADD COLUMN {field} INT NOT NULL DEFAULT 0')

        customer_columns = {
            'citizen_id': "VARCHAR(20) NULL",
            'birth_date': "DATE NULL",
            'address': "TEXT NULL",
        }
        for field, sql_type in customer_columns.items():
            found = conn.execute(
                'SELECT COUNT(*) FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=?',
                ('customers', field)
            ).fetchone()[0]
            if not found:
                conn.execute(f'ALTER TABLE customers ADD COLUMN {field} {sql_type}')

        conn.execute("""CREATE TABLE IF NOT EXISTS app_meta (
            meta_key VARCHAR(100) PRIMARY KEY,
            meta_value VARCHAR(255) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        migrated = conn.execute(
            'SELECT meta_value FROM app_meta WHERE meta_key=?', ('seed_12_rooms_v1',)
        ).fetchone()
        if not migrated:
            extra_rooms = [
                ('103','Standard',800000), ('104','Standard',800000),
                ('105','Standard',800000), ('106','Standard',800000),
                ('203','Superior',1100000), ('204','Superior',1100000),
                ('205','Superior',1100000),
                ('303','Deluxe',1500000), ('304','Deluxe',1500000),
                ('402','Suite',2500000), ('403','Suite',2500000),
                ('502','Villa',4500000),
            ]
            conn.executemany(
                """INSERT IGNORE INTO rooms(number,room_type,price,status,note)
                VALUES(?,?,?,?,?)""",
                [(n, t, p, 'Sạch', '') for n,t,p in extra_rooms]
            )
            conn.execute(
                'INSERT INTO app_meta(meta_key,meta_value) VALUES(?,?)',
                ('seed_12_rooms_v1', 'done')
            )

def rooms():
    return read('''SELECT r.*, CASE WHEN EXISTS
        (SELECT 1 FROM bookings b WHERE b.room_id=r.id AND b.status='Đang ở')
        THEN 'Có khách' WHEN EXISTS
        (SELECT 1 FROM bookings b WHERE b.room_id=r.id AND b.status='Đã đặt'
         AND b.checkin <= CURRENT_DATE() AND b.checkout > CURRENT_DATE())
        THEN 'Đã đặt hôm nay' ELSE r.status END AS display_status
        FROM rooms r ORDER BY r.number''')

def customers():
    return read('SELECT * FROM customers ORDER BY id DESC')

def customers_with_rooms():
    return read('''SELECT c.id, c.name, c.phone, c.email,
        c.citizen_id, c.birth_date, c.address,
        COALESCE(GROUP_CONCAT(DISTINCT CASE
            WHEN b.status IN ('Đã đặt', 'Đang ở') THEN r.number
            ELSE NULL END ORDER BY r.number SEPARATOR ', '), 'Chưa đặt') AS room_numbers
        FROM customers c
        LEFT JOIN bookings b ON b.customer_id=c.id
        LEFT JOIN rooms r ON r.id=b.room_id
        GROUP BY c.id, c.name, c.phone, c.email,
                 c.citizen_id, c.birth_date, c.address
        ORDER BY c.id DESC''')

def bookings():
    return read('''SELECT b.*, c.name AS customer, c.phone, r.number AS room,
        r.room_type FROM bookings b JOIN customers c ON c.id=b.customer_id
        JOIN rooms r ON r.id=b.room_id ORDER BY b.id DESC''')

def vnd(amount):
    return f'{int(amount):,} VNĐ'

def table(df):
    st.dataframe(df, use_container_width=True, hide_index=True)

def booking_conflict(conn, room_id, start, end, exclude=None):
    sql = '''SELECT COUNT(*) FROM bookings WHERE room_id=?
        AND status IN ('Đã đặt','Đang ở') AND checkin<? AND checkout>?'''
    params = [room_id, str(end), str(start)]
    if exclude is not None:
        sql += ' AND id<>?'
        params.append(exclude)
    return conn.execute(sql, params).fetchone()[0] > 0

def message_and_reload(text):
    st.session_state['flash_message'] = text
    st.rerun()

def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, 310000)
    return 'pbkdf2_sha256$310000$' + salt.hex() + '$' + digest.hex()

def verify_password(password, encoded):
    try:
        algorithm, rounds, salt_hex, digest_hex = encoded.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        actual = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'),
                                    bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(actual, bytes.fromhex(digest_hex))
    except (ValueError, TypeError):
        return False

# --- HÀM TRỢ LÝ AI GEMINI THÔNG MINH ---
def generate_bot_response(prompt):
    # Dữ liệu thực tế từ hệ thống
    r_df = rooms()
    b_df = bookings()
    active_b = b_df[b_df.status == 'Đang ở']
    paid_b = b_df[b_df.status == 'Đã trả']
    clean_rooms = r_df[r_df.display_status == 'Sạch']
    
    # 1. Nếu đã cài GEMINI_API_KEY -> Xử lý bằng AI Gemini 2.5 Flash
    if GEMINI_KEY:
        try:
            client = genai.Client(api_key=GEMINI_KEY)
            
            context = f"""
            Bạn là Trợ lý AI thông minh phục vụ nội bộ Khách sạn Hi Vọng.
            Hãy trả lời ngắn gọn, thân thiện, chính xác bằng tiếng Việt dựa vào dữ liệu hệ thống thời gian thực dưới đây:

            --- BẢNG GIÁ NIÊM YẾT ---
            {TYPES}

            --- TRẠNG THÁI PHÒNG THỰC TẾ ---
            - Tổng số phòng: {len(r_df)} phòng
            - Phòng trống/sạch ({len(clean_rooms)} phòng): {", ".join(clean_rooms['number'].tolist()) if not clean_rooms.empty else "Không có"}
            - Chi tiết tất cả phòng:
            {r_df[['number', 'room_type', 'price', 'display_status']].to_string(index=False)}

            --- THÔNG TIN LƯU TRÚ & DOANH THU ---
            - Số phòng đang có khách ({len(active_b)} phòng): {", ".join([f"Phòng {x.room} ({x.customer})" for x in active_b.itertuples()]) if not active_b.empty else "Không có"}
            - Tổng số khách đang ở: {int(active_b.adults.sum() + active_b.children.sum()) if not active_b.empty else 0} người
            - Doanh thu thực tế đã thu: {paid_b['total'].sum():,} VNĐ (từ {len(paid_b)} đơn checkout)

            --- QUY TRÌNH NỘI BỘ ---
            1. Check-in:
               - Thời gian tiêu chuẩn: Từ 14:00.
               - Nhận phòng sớm: 06:00 - 10:00 (phụ thu 50%), 10:00 - 14:00 (phụ thu 30%).
               - Điều kiện: Phòng phải ở trạng thái "Sạch" và xác minh CCCD/Hộ chiếu khách.
            2. Check-out:
               - Thời gian tiêu chuẩn: Trước 12:00 trưa.
               - Trả phòng trễ: 12:00 - 15:00 (phụ thu 30%), 15:00 - 18:00 (phụ thu 50%), Sau 18:00 (tính 100% đêm).
               - Quy trình: Báo buồng phòng kiểm tra minibar -> Thu tiền -> Hệ thống chuyển trạng thái phòng sang "Bẩn".
            3. Buồng phòng & Đồ thất lạc (Lost & Found):
               - Trạng thái phòng: Sạch, Bẩn, Đang vệ sinh, Bảo trì.
               - Đồ bỏ quên: Niêm phong ghi rõ (Số phòng, Ngày, Nhân viên) -> Bàn giao Lễ tân -> Liên hệ khách.
            4. Hotline khẩn cấp:
               - Quản lý ca: 0901.234.567 | Kỹ thuật: Nhánh 102 (0902.111.222) | Bảo vệ: Nhánh 100 | PCCC: 114.
            """
            
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=f"{context}\n\nCâu hỏi của nhân viên: {prompt}"
            )
            return response.text
        except Exception as e:
            return f"⚠️ Lỗi kết nối AI Gemini ({str(e)}). Đang chuyển sang chế độ dự phòng...\n\n" + _fallback_response(prompt, r_df, b_df)

    # 2. Nếu chưa cấu hình GEMINI_API_KEY -> Chạy hàm dự phòng thông minh hơn
    return _fallback_response(prompt, r_df, b_df)

def _fallback_response(prompt, r_df, b_df):
    p = prompt.lower()
    
    # Số lượng phòng
    if any(k in p for k in ["bao nhiêu phòng", "mấy phòng", "tổng phòng", "có bao nhiu phòng", "số phòng"]):
        return f"🏨 Khách sạn Hi Vọng hiện có tổng cộng **{len(r_df)} phòng**."
    
    # Rẻ nhất / Đắt nhất
    elif any(k in p for k in ["rẻ nhất", "thấp nhất"]):
        cheapest = min(TYPES.items(), key=lambda x: x[1])
        return f"🏷️ Phòng có giá rẻ nhất là hạng **{cheapest[0]}** với giá **{cheapest[1]:,} VNĐ/đêm**."
    elif any(k in p for k in ["đắt nhất", "cao nhất"]):
        expensive = max(TYPES.items(), key=lambda x: x[1])
        return f"💎 Hạng phòng cao cấp nhất là **{expensive[0]}** với giá **{expensive[1]:,} VNĐ/đêm**."
        
    # Phòng trống
    elif any(k in p for k in ["trống", "rảnh", "sẵn sàng", "sạch"]):
        clean = r_df[r_df.display_status == 'Sạch']
        if not clean.empty:
            list_str = ", ".join([f"Phòng {x.number} ({x.room_type})" for x in clean.itertuples()])
            return f"🏨 Có **{len(clean)} phòng sạch/trống** sẵn sàng đón khách:\n👉 {list_str}"
        return "⚠️ Hiện tại không còn phòng trống ở trạng thái Sạch."
        
    # Giá phòng
    elif any(k in p for k in ["giá", "bao nhiêu", "bảng giá", "loại phòng"]):
        prices = "\n".join([f"- **{k}**: {v:,} VNĐ/đêm" for k, v in TYPES.items()])
        return f"📋 **Bảng giá phòng niêm yết:**\n\n{prices}"
        
    # Khách đang ở
    elif any(k in p for k in ["đang ở", "khách ở"]):
        active = b_df[b_df.status == 'Đang ở']
        if not active.empty:
            list_str = ", ".join([f"Phòng {x.room} ({x.customer})" for x in active.itertuples()])
            return f"👥 Hiện có **{len(active)} phòng đang có khách**:\n👉 {list_str}"
        return "ℹ️ Hiện không có khách đang lưu trú."
        
    # Doanh thu
    elif any(k in p for k in ["doanh thu", "tổng thu"]):
        paid = b_df[b_df.status == 'Đã trả']
        return f"💰 Tổng doanh thu thực tế đã thu: **{paid['total'].sum():,} VNĐ**."
        
    # Quy trình Checkin / Checkout / Hotline
    elif "checkin" in p or "nhận phòng" in p:
        return "🔑 **Check-in:** Tiêu chuẩn từ 14:00. Nhận sớm 06:00-10:00 (phụ thu 50%), 10:00-14:00 (phụ thu 30%). Yêu cầu phòng Sạch & kiểm tra CCCD."
    elif "checkout" in p or "trả phòng" in p:
        return "🚪 **Check-out:** Tiêu chuẩn trước 12:00. Trễ 12:00-15:00 (+30%), 15:00-18:00 (+50%). Báo buồng phòng kiểm tra trước khi thu tiền."
    elif any(k in p for k in ["hotline", "khẩn cấp", "sđt", "bảo vệ"]):
        return "📞 **Hotline:** Quản lý ca (0901.234.567) | Kỹ thuật (Nhánh 102) | Bảo vệ (Nhánh 100)."
        
    return (
        "🤖 **Trợ lý Khách sạn Hi Vọng**\n\n"
        "Bạn có thể hỏi tôi các câu hỏi như:\n"
        "- *Khách sạn có bao nhiêu phòng?*\n"
        "- *Phòng nào rẻ nhất / đắt nhất?*\n"
        "- *Danh sách phòng trống hôm nay?*\n"
        "- *Quy trình checkin, checkout, hotline khẩn cấp...*"
    )

def login_screen():
    st.title('🏨 KHÁCH SẠN HI VỌNG')
    st.subheader('Đăng nhập nhân viên')
    with connect() as conn:
        count = conn.execute('SELECT COUNT(*) FROM employees').fetchone()[0]
    if count == 0:
        setup_token = st.secrets.get('ADMIN_SETUP_TOKEN', os.environ.get('ADMIN_SETUP_TOKEN', ''))
        if not setup_token:
            st.warning('Chưa có tài khoản quản trị. Vui lòng cấu hình ADMIN_SETUP_TOKEN trong Secrets trước khi tiếp tục.')
            st.stop()
        st.info('Thiết lập tài khoản quản trị lần đầu.')
        with st.form('first_admin'):
            token = st.text_input('Mã thiết lập quản trị', type='password')
            username = st.text_input('Tên đăng nhập quản trị').strip().lower()
            full_name = st.text_input('Họ và tên')
            password = st.text_input('Mật khẩu mới (ít nhất 12 ký tự)', type='password')
            confirm = st.text_input('Nhập lại mật khẩu', type='password')
            submit = st.form_submit_button('Tạo tài khoản quản trị', type='primary')
        if submit:
            if not hmac.compare_digest(token, setup_token):
                st.error('Mã thiết lập không đúng.')
            elif not username or not full_name.strip() or len(password) < 12 or password != confirm:
                st.error('Nhập đủ thông tin; mật khẩu tối thiểu 12 ký tự và phải khớp.')
            else:
                try:
                    with connect() as conn:
                        conn.execute('INSERT INTO employees(username,full_name,password_hash,role) VALUES(?,?,?,?)',
                                     (username, full_name.strip(), hash_password(password), 'admin'))
                    st.success('Đã tạo tài khoản. Vui lòng đăng nhập.')
                    st.rerun()
                except mysql.connector.IntegrityError:
                    st.error('Tên đăng nhập đã tồn tại.')
        st.stop()
    with st.form('employee_login'):
        username = st.text_input('Tên đăng nhập').strip().lower()
        password = st.text_input('Mật khẩu', type='password')
        submit = st.form_submit_button('Đăng nhập', type='primary')
    if submit:
        with connect() as conn:
            employee = conn.execute('SELECT * FROM employees WHERE username=? AND active=1',
                                    (username,)).fetchone()
        if employee and verify_password(password, employee['password_hash']):
            st.session_state['employee'] = {'id': employee['id'], 'name': employee['full_name'],
                                            'role': employee['role']}
            st.rerun()
        else:
            st.error('Sai tên đăng nhập, mật khẩu hoặc tài khoản đã bị khóa.')
    st.stop()

if not DB_PASSWORD:
    st.error('Thiếu DB_PASSWORD trong Streamlit Secrets.')
    st.stop()

try:
    with connect() as _db:
        _db.execute('SELECT 1').fetchone()
    initialize()
except mysql.connector.Error as exc:
    st.error('🔴 Không thể kết nối MySQL Aiven hoặc khởi tạo database.')
    st.code(f'Mã lỗi: {exc.errno} | {exc.msg}')
    st.stop()

if 'employee' not in st.session_state:
    login_screen()

st.markdown('''<style>
.block-container{padding-top:1.3rem}
h1,h2,h3{color:#16375d}
[data-testid="stMetric"]{background:#edf3fa;border-radius:12px;padding:16px}
</style>''', unsafe_allow_html=True)

with st.sidebar:
    st.title('🏨 HI VỌNG HOTEL')
    st.caption('Nhân viên: ' + st.session_state['employee']['name'])
    if st.button('🚪 Đăng xuất'):
        st.session_state.clear()
        st.rerun()
    menu_items = ['📊 Tổng quan', '🛏️ Quản lý phòng',
        '📅 Đặt phòng', '🔑 Nhận / Trả phòng', '🧹 Buồng phòng',
        '👥 Khách hàng', '💰 Doanh thu', '🤖 Trợ lý AI & Quy trình']
    if st.session_state['employee']['role'] == 'admin':
        menu_items.append('🔐 Nhân viên')
    menu = st.radio('Điều hướng', menu_items, key='main_menu')
    st.success('🟢 Đã kết nối MySQL Aiven')
    if GEMINI_KEY:
        st.info('✨ Đã bật Google Gemini AI')

_flash = st.session_state.pop('flash_message', None)
if _flash:
    st.success(_flash)

if menu == '📊 Tổng quan':
    st.title('🏨 KHÁCH SẠN HI VỌNG')
    if IMAGE.exists():
        st.image(str(IMAGE), caption='Khách sạn Hi Vọng', use_container_width=True)
    else:
        st.info('Đặt ảnh khachsan.jpg cùng thư mục với app.py để hiển thị ảnh khách sạn.')
    st.subheader('Tổng quan Khách sạn Hi Vọng')
    r, b = rooms(), bookings()
    occupied = int((r.display_status == 'Có khách').sum())
    clean = int((r.display_status == 'Sạch').sum())
    pending = b[b.status == 'Đã đặt']
    active = b[b.status == 'Đang ở']
    completed = b[b.status == 'Đã trả']
    revenue = int(completed.total.sum())
    expected = int(pending.total.sum() + active.total.sum())
    a, c, d, e = st.columns(4)
    a.metric('Tổng số phòng', len(r))
    c.metric('Đơn đặt chờ nhận', len(pending))
    d.metric('Đang có khách (phòng)', occupied)
    e.metric('Phòng sạch chưa có khách', clean)
    f, g, h = st.columns(3)
    f.metric('Khách đang lưu trú (người)', int((active.adults + active.children).sum()))
    g.metric('Doanh thu dự kiến (chưa trả)', vnd(expected))
    h.metric('Doanh thu đã ghi nhận', vnd(revenue))
    st.metric('Công suất phòng hiện tại', f'{occupied / len(r) * 100:.1f}%' if len(r) else '0%')
    if not r.empty:
        counts = r.display_status.value_counts().rename_axis('Trạng thái').reset_index(name='Số phòng')
        st.plotly_chart(px.pie(counts, names='Trạng thái', values='Số phòng', hole=.4),
                        use_container_width=True)
    st.subheader('Sơ đồ trạng thái phòng')
    table(r[['number','room_type','price','display_status']].rename(columns={
        'number':'Phòng','room_type':'Loại','price':'Giá/đêm','display_status':'Trạng thái'}))

elif menu == '🛏️ Quản lý phòng':
    st.title('🛏️ Quản lý phòng')
    tab1, tab2, tab3 = st.tabs(['Danh sách', 'Thêm phòng', 'Sửa / Xóa'])
    with tab1:
        r = rooms()
        kind = st.selectbox('Lọc loại phòng', ['Tất cả'] + list(TYPES))
        if kind != 'Tất cả':
            r = r[r.room_type == kind]
        table(r[['number','room_type','price','display_status','note']])
    with tab2:
        with st.form('add_room'):
            number = st.text_input('Số phòng')
            kind = st.selectbox('Loại phòng', list(TYPES))
            price = st.number_input('Giá mỗi đêm (VNĐ)', min_value=0, value=800000, step=100000)
            status = st.selectbox('Tình trạng', HOUSEKEEPING)
            note = st.text_area('Ghi chú')
            if st.form_submit_button('Thêm phòng', type='primary'):
                if not number.strip():
                    st.error('Vui lòng nhập số phòng.')
                else:
                    try:
                        write('INSERT INTO rooms(number,room_type,price,status,note) VALUES(?,?,?,?,?)',
                              (number.strip(),kind,price,status,note))
                        message_and_reload('Đã thêm phòng.')
                    except mysql.connector.IntegrityError:
                        st.error('Số phòng đã tồn tại.')
    with tab3:
        r = rooms()
        if not r.empty:
            choices = {f'{x.number} – {x.room_type}': int(x.id) for x in r.itertuples()}
            room_id = choices[st.selectbox('Chọn phòng', list(choices))]
            room = r.loc[r.id == room_id].iloc[0]
            with st.form('edit_room'):
                number = st.text_input('Số phòng', value=room.number)
                kind = st.selectbox('Loại phòng', list(TYPES), index=list(TYPES).index(room.room_type))
                price = st.number_input('Giá/đêm', min_value=0, value=int(room.price), step=100000)
                status = st.selectbox('Tình trạng', HOUSEKEEPING, index=HOUSEKEEPING.index(room.status))
                note = st.text_area('Ghi chú', value=room.note)
                if st.form_submit_button('Lưu thay đổi', type='primary'):
                    if not number.strip():
                        st.error('Số phòng không được trống.')
                    elif room.display_status == 'Có khách' and status == 'Bảo trì':
                        st.error('Không chuyển phòng đang có khách sang bảo trì.')
                    else:
                        try:
                            write('UPDATE rooms SET number=?,room_type=?,price=?,status=?,note=? WHERE id=?',
                                  (number.strip(),kind,price,status,note,room_id))
                            message_and_reload('Đã cập nhật phòng.')
                        except mysql.connector.IntegrityError:
                            st.error('Số phòng đã tồn tại.')
            confirm = st.checkbox('Xác nhận xóa phòng này')
            if st.button('Xóa phòng', disabled=not confirm):
                with connect() as conn:
                    used = conn.execute('SELECT COUNT(*) FROM bookings WHERE room_id=?',(room_id,)).fetchone()[0]
                    if used:
                        st.error('Không thể xóa phòng đã có lịch sử đặt phòng.')
                    else:
                        conn.execute('DELETE FROM rooms WHERE id=?',(room_id,))
                if not used:
                    message_and_reload('Đã xóa phòng.')

elif menu == '📅 Đặt phòng':
    st.title('📅 Đặt phòng')
    tab1, tab2 = st.tabs(['Tạo đặt phòng', 'Danh sách / Hủy'])
    with tab1:
        c, r = customers(), rooms()
        if c.empty:
            st.warning('Hãy thêm khách hàng trong mục Khách hàng trước.')
        elif r.empty:
            st.warning('Chưa có phòng.')
        else:
            customer_options = {f'{x.id} – {x.name}':int(x.id) for x in c.itertuples()}
            room_options = {f'{x.number} – {x.room_type} – {vnd(x.price)}/đêm':int(x.id)
                            for x in r.itertuples() if x.status != 'Bảo trì'}
            new_customer_id = st.session_state.get('booking_customer_id')
            customer_labels = list(customer_options)
            default_index = next((i for i, label in enumerate(customer_labels)
                                  if customer_options[label] == new_customer_id), 0)
            if new_customer_id is not None:
                st.info('Đã lưu khách hàng. Vui lòng chọn phòng và ngày lưu trú để hoàn tất đặt phòng.')
            with st.form('create_booking'):
                customer_label = st.selectbox('Khách hàng', customer_labels, index=default_index)
                start = st.date_input('Ngày nhận', today())
                end = st.date_input('Ngày trả', today()+timedelta(days=1))
                room_label = st.selectbox('Phòng', list(room_options)) if room_options else None
                adults = st.number_input('Số người lớn', min_value=1, max_value=20, value=2)
                children = st.number_input('Số trẻ em', min_value=0, max_value=20, value=0)
                note = st.text_area('Yêu cầu đặc biệt')
                if st.form_submit_button('Xác nhận đặt', type='primary', disabled=not room_options):
                    room_id = room_options[room_label]
                    if start < today() or end <= start:
                        st.error('Ngày nhận không được trong quá khứ và ngày trả phải sau ngày nhận.')
                    else:
                        with connect() as conn:
                            current = conn.execute('SELECT * FROM rooms WHERE id=? FOR UPDATE',(room_id,)).fetchone()
                            if current['status'] == 'Bảo trì' or booking_conflict(conn,room_id,start,end):
                                st.error('Phòng bảo trì hoặc đã có lịch đặt trùng thời gian.')
                            else:
                                total = (end-start).days * current['price']
                                conn.execute('''INSERT INTO bookings
                                    (customer_id,room_id,checkin,checkout,total,note,adults,children)
                                    VALUES(?,?,?,?,?,?,?,?)''',
                                    (customer_options[customer_label],room_id,str(start),str(end),total,note,int(adults),int(children)))
                        st.session_state.pop('booking_customer_id', None)
                        message_and_reload(f'Đặt phòng thành công. Dự kiến: {vnd(total)}')
    with tab2:
        b = bookings()
        if not b.empty:
            status = st.selectbox('Lọc trạng thái', ['Tất cả','Đã đặt','Đang ở','Đã trả','Đã hủy'])
            shown = b if status == 'Tất cả' else b[b.status == status]
            table(shown[['id','customer','phone','room','checkin','checkout','status','total']])
            pending = b[b.status == 'Đã đặt']
            if not pending.empty:
                choices = {f'#{x.id} – {x.customer} – Phòng {x.room}':int(x.id)
                           for x in pending.itertuples()}
                selected = choices[st.selectbox('Chọn đặt phòng để hủy',list(choices))]
                if st.button('Hủy đặt phòng'):
                    write("UPDATE bookings SET status='Đã hủy' WHERE id=? AND status='Đã đặt'",(selected,))
                    message_and_reload('Đã hủy đặt phòng.')
        else:
            st.info('Chưa có đặt phòng.')

elif menu == '🔑 Nhận / Trả phòng':
    st.title('🔑 Nhận / Trả phòng')
    b = bookings()
    tab1, tab2 = st.tabs(['Check-in', 'Check-out'])
    with tab1:
        pending = b[b.status == 'Đã đặt']
        if pending.empty:
            st.info('Không có đặt phòng chờ nhận.')
        else:
            choices = {f'#{x.id} – {x.customer} – Phòng {x.room}':int(x.id)
                       for x in pending.itertuples()}
            bid = choices[st.selectbox('Khách nhận phòng',list(choices))]
            item = pending.loc[pending.id == bid].iloc[0]
            st.write(f"Ngày nhận: {item.checkin} | Ngày trả: {item.checkout}")
            if st.button('Xác nhận Check-in', type='primary'):
                with connect() as conn:
                    room = conn.execute('SELECT status FROM rooms WHERE id=? FOR UPDATE',(int(item.room_id),)).fetchone()
                    occupied = conn.execute("SELECT COUNT(*) FROM bookings WHERE room_id=? AND status='Đang ở'",
                                            (int(item.room_id),)).fetchone()[0]
                    if room['status'] != 'Sạch' or occupied:
                        st.error('Phòng chưa sạch hoặc đang có khách.')
                    elif not (date.fromisoformat(item.checkin) <= today() < date.fromisoformat(item.checkout)):
                        st.error('Chỉ nhận phòng trong khoảng ngày đặt. Hãy điều chỉnh đặt phòng nếu cần.')
                    else:
                        result = conn.execute("UPDATE bookings SET status='Đang ở' WHERE id=? AND status='Đã đặt'",(bid,))
                        if result.rowcount != 1:
                            raise ValueError('Đơn đặt phòng đã thay đổi, vui lòng tải lại.')
                if room['status'] == 'Sạch' and not occupied and (date.fromisoformat(item.checkin) <= today() < date.fromisoformat(item.checkout)):
                    message_and_reload('Check-in thành công.')
    with tab2:
        active = b[b.status == 'Đang ở']
        if active.empty:
            st.info('Không có khách đang lưu trú.')
        else:
            choices = {f'#{x.id} – {x.customer} – Phòng {x.room}':int(x.id)
                       for x in active.itertuples()}
            bid = choices[st.selectbox('Khách trả phòng',list(choices))]
            item = active.loc[active.id == bid].iloc[0]
            st.write(f'Tiền phòng dự kiến: {vnd(item.total)}')
            total = st.number_input('Số tiền thanh toán thực tế (VNĐ)',
                                    min_value=0, value=int(item.total), step=100000)
            if st.button('Xác nhận Check-out', type='primary'):
                with connect() as conn:
                    conn.execute("UPDATE bookings SET status='Đã trả',actual_checkout=?,total=? WHERE id=? AND status='Đang ở'",
                                 (str(today()),total,bid))
                    conn.execute("UPDATE rooms SET status='Bẩn' WHERE id=?",(int(item.room_id),))
                message_and_reload('Check-out thành công. Phòng đã chuyển sang trạng thái Bẩn.')

elif menu == '🧹 Buồng phòng':
    st.title('🧹 Quản lý buồng phòng')
    r = rooms()
    cols = st.columns(4)
    for col, status in zip(cols,HOUSEKEEPING):
        col.metric(status,int((r.display_status == status).sum()))
    table(r[['number','room_type','display_status','note']])
    if not r.empty:
        choices = {f'{x.number} – {x.display_status}':int(x.id) for x in r.itertuples()}
        room_id = choices[st.selectbox('Chọn phòng cập nhật',list(choices))]
        new_status = st.selectbox('Tình trạng mới',HOUSEKEEPING)
        if st.button('Cập nhật tình trạng',type='primary'):
            with connect() as conn:
                occupied = conn.execute("SELECT COUNT(*) FROM bookings WHERE room_id=? AND status='Đang ở'",
                                        (room_id,)).fetchone()[0]
                if occupied:
                    st.error('Phòng đang có khách. Không thay đổi trạng thái bằng màn hình này.')
                else:
                    conn.execute('UPDATE rooms SET status=? WHERE id=?',(new_status,room_id))
            if not occupied:
                message_and_reload('Đã cập nhật tình trạng phòng.')

elif menu == '👥 Khách hàng':
    st.title('👥 Khách hàng')
    tab1, tab2 = st.tabs(['Danh sách', 'Thêm khách hàng'])
    with tab1:
        c = customers_with_rooms()
        keyword = st.text_input('Tìm tên, số điện thoại hoặc số phòng').strip()
        if keyword and not c.empty:
            c = c[c.name.str.contains(keyword, case=False, regex=False, na=False) |
                  c.phone.str.contains(keyword, case=False, regex=False, na=False) |
                  c.room_numbers.str.contains(keyword, case=False, regex=False, na=False)]
        if not c.empty:
            c = c.copy()
            c['citizen_id'] = c.citizen_id.fillna('').apply(
                lambda x: ('*' * max(0, len(str(x)) - 4) + str(x)[-4:]) if x else '')
            c['birth_date'] = c.birth_date.fillna('').astype(str)
            table(c.rename(columns={
                'id': 'Mã KH', 'name': 'Họ tên', 'phone': 'Số điện thoại',
                'email': 'Email', 'citizen_id': 'CCCD (ẩn bớt)',
                'birth_date': 'Ngày sinh', 'address': 'Địa chỉ',
                'room_numbers': 'Số phòng'
            }))
        else:
            st.info('Chưa có khách hàng phù hợp.')
    with tab2:
        st.caption('Sau khi lưu thành công, ứng dụng tự chuyển sang Đặt phòng và chọn sẵn khách vừa tạo.')
        with st.form('new_customer'):
            name = st.text_input('Họ tên *')
            citizen_id = st.text_input('Số căn cước công dân', max_chars=12)
            birth_date = st.date_input('Ngày tháng năm sinh', value=None,
                                       min_value=date(1900, 1, 1), max_value=today(),
                                       format='DD/MM/YYYY')
            address = st.text_area('Địa chỉ')
            phone = st.text_input('Số điện thoại')
            email = st.text_input('Email')
            if st.form_submit_button('Lưu khách hàng và chuyển sang đặt phòng', type='primary'):
                name = name.strip()
                citizen_id = citizen_id.strip()
                if not name:
                    st.error('Vui lòng nhập họ tên.')
                elif citizen_id and (len(citizen_id) != 12 or not citizen_id.isdigit()):
                    st.error('Số CCCD phải gồm đúng 12 chữ số.')
                elif birth_date and birth_date > today():
                    st.error('Ngày sinh không được ở tương lai.')
                else:
                    try:
                        with connect() as conn:
                            if citizen_id:
                                duplicate = conn.execute(
                                    'SELECT id FROM customers WHERE citizen_id=? LIMIT 1',
                                    (citizen_id,)
                                ).fetchone()
                                if duplicate:
                                    st.error('CCCD đã tồn tại. Vui lòng kiểm tra khách hàng trong danh sách.')
                                    st.stop()
                            new_id = conn.execute(
                                'INSERT INTO customers(name,phone,email,citizen_id,birth_date,address) '
                                'VALUES(?,?,?,?,?,?)',
                                (name, phone.strip(), email.strip(), citizen_id or None, birth_date, address.strip() or None)
                            ).lastrowid
                        st.session_state['booking_customer_id'] = new_id
                        st.session_state['main_menu'] = '📅 Đặt phòng'
                        message_and_reload(f'Đã lưu khách hàng "{name}". Chuyển sang tạo đặt phòng.')
                    except mysql.connector.Error as exc:
                        st.error(f'Lỗi cơ sở dữ liệu: {exc.msg}')

elif menu == '💰 Doanh thu':
    st.title('💰 Doanh thu')
    b = bookings()
    if b.empty or (b.status == 'Đã trả').sum() == 0:
        st.info('Chưa có dữ liệu doanh thu từ các đơn đã checkout.')
    else:
        paid = b[b.status == 'Đã trả'].copy()
        paid['checkout_date'] = paid['actual_checkout'].fillna(paid['checkout'])
        
        st.subheader('Tổng quan doanh thu')
        col1, col2, col3 = st.columns(3)
        col1.metric('Tổng doanh thu đã thu', vnd(paid['total'].sum()))
        col2.metric('Tổng số lượt đặt phòng hoàn tất', len(paid))
        col3.metric('Giá trị trung bình / đơn', vnd(paid['total'].mean()))
        
        st.subheader('Doanh thu theo loại phòng')
        rev_by_type = paid.groupby('room_type')['total'].sum().reset_index()
        rev_by_type.columns = ['Loại phòng', 'Doanh thu (VNĐ)']
        st.plotly_chart(px.bar(rev_by_type, x='Loại phòng', y='Doanh thu (VNĐ)', color='Loại phòng', text_auto=True), use_container_width=True)
        
        st.subheader('Lịch sử thanh toán')
        table(paid[['id', 'customer', 'room', 'checkin', 'actual_checkout', 'total']].rename(columns={
            'id': 'Mã đơn', 'customer': 'Khách hàng', 'room': 'Phòng',
            'checkin': 'Ngày check-in', 'actual_checkout': 'Ngày check-out', 'total': 'Tổng tiền'
        }))

elif menu == '🤖 Trợ lý AI & Quy trình':
    st.title('🤖 Trợ lý AI & Quy trình Nội bộ')
    st.caption('Trợ lý Gemini AI hỗ trợ nhân viên tra cứu nhanh dữ liệu phòng thực tế và quy trình vận hành khách sạn.')
    
    st.markdown("**Gợi ý tra cứu nhanh:**")
    col1, col2, col3, col4, col5 = st.columns(5)
    btn_prompt = None
    if col1.button("🔑 Quy trình Checkin"):
        btn_prompt = "quy trình checkin"
    if col2.button("🚪 Quy trình Checkout"):
        btn_prompt = "quy trình checkout"
    if col3.button("🛏️ Tìm phòng trống"):
        btn_prompt = "phòng trống"
    if col4.button("🏷️ Phòng rẻ nhất"):
        btn_prompt = "phòng nào rẻ nhất"
    if col5.button("📞 Hotline khẩn cấp"):
        btn_prompt = "hotline"

    if "messages" not in st.session_state:
        st.session_state.messages = [
            {"role": "assistant", "content": "Xin chào! Tôi là Trợ lý AI Gemini của Khách sạn Hi Vọng. Bạn cần tôi hỗ trợ tra cứu phòng trống, giá phòng hay quy trình gì hôm nay?"}
        ]

    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg["content"])

    prompt = btn_prompt or st.chat_input("Nhập câu hỏi (VD: phòng rẻ nhất, có bao nhiêu phòng, checkin, hotline...)...")
    if prompt:
        st.session_state.messages.append({"role": "user", "content": prompt})
        with st.chat_message("user"):
            st.write(prompt)

        with st.spinner("Gemini đang suy nghĩ..."):
            response = generate_bot_response(prompt)

        with st.chat_message("assistant"):
            st.write(response)
        st.session_state.messages.append({"role": "assistant", "content": response})

elif menu == '🔐 Nhân viên' and st.session_state['employee']['role'] == 'admin':
    st.title('🔐 Quản lý nhân viên')
    tab1, tab2 = st.tabs(['Danh sách nhân viên', 'Thêm nhân viên mới'])
    
    with tab1:
        employees_df = read('SELECT id, username, full_name, role, active FROM employees ORDER BY id DESC')
        if not employees_df.empty:
            employees_df['active_status'] = employees_df['active'].apply(lambda x: 'Hoạt động' if x == 1 else 'Đã khóa')
            table(employees_df[['id', 'username', 'full_name', 'role', 'active_status']].rename(columns={
                'id': 'ID', 'username': 'Tên đăng nhập', 'full_name': 'Họ và tên',
                'role': 'Vai trò', 'active_status': 'Trạng thái'
            }))
            
            st.subheader('Cập nhật trạng thái / Vai trò')
            emp_choices = {f"{row.username} ({row.full_name})": row.id for row in employees_df.itertuples()}
            selected_emp = st.selectbox('Chọn nhân viên', list(emp_choices))
            selected_id = emp_choices[selected_emp]
            
            with st.form('update_employee'):
                new_role = st.selectbox('Vai trò', ['staff', 'admin'])
                new_active = st.selectbox('Trạng thái', ['Hoạt động', 'Đã khóa'])
                if st.form_submit_button('Lưu thay đổi', type='primary'):
                    if selected_id == st.session_state['employee']['id'] and new_active == 'Đã khóa':
                        st.error('Không thể tự khóa tài khoản của chính mình.')
                    else:
                        active_val = 1 if new_active == 'Hoạt động' else 0
                        write('UPDATE employees SET role=?, active=? WHERE id=?', (new_role, active_val, selected_id))
                        message_and_reload('Đã cập nhật thông tin nhân viên.')
        else:
            st.info('Chưa có dữ liệu nhân viên.')
            
    with tab2:
        with st.form('add_employee'):
            username = st.text_input('Tên đăng nhập *').strip().lower()
            full_name = st.text_input('Họ và tên *').strip()
            password = st.text_input('Mật khẩu * (Tối thiểu 12 ký tự)', type='password')
            role = st.selectbox('Vai trò', ['staff', 'admin'])
            if st.form_submit_button('Tạo tài khoản', type='primary'):
                if not username or not full_name:
                    st.error('Vui lòng điền đầy đủ tên đăng nhập và họ tên.')
                elif len(password) < 12:
                    st.error('Mật khẩu phải có ít nhất 12 ký tự.')
                else:
                    try:
                        write('INSERT INTO employees(username, full_name, password_hash, role) VALUES(?,?,?,?)',
                              (username, full_name, hash_password(password), role))
                        message_and_reload(f'Đã tạo tài khoản nhân viên "{username}" thành công.')
                    except mysql.connector.IntegrityError:
                        st.error('Tên đăng nhập đã tồn tại trong hệ thống.')

# --- Widget Chatbot Tra cứu nhanh ở góc màn hình ---
if menu != '🤖 Trợ lý AI & Quy trình':
    with st.popover("💬 Trợ lý Gemini AI", use_container_width=False):
        st.subheader("🤖 Tra cứu nhanh")
        q = st.text_input("Hỏi Gemini AI (VD: phòng rẻ nhất, bao nhiêu phòng, checkin...):")
        if q:
            with st.spinner("AI đang trả lời..."):
                st.markdown(generate_bot_response(q))
