from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
import mysql.connector
import pandas as pd
import plotly.express as px
import streamlit as st

BASE = Path(__file__).resolve().parent
IMAGE = BASE / 'khachsan.jpg'
TYPES = {'Standard': 800_000, 'Superior': 1_100_000, 'Deluxe': 1_500_000,
         'Suite': 2_500_000, 'Villa': 4_500_000}
HOUSEKEEPING = ['Sạch', 'Bẩn', 'Đang vệ sinh', 'Bảo trì']

def today():
    return datetime.now(ZoneInfo('Asia/Ho_Chi_Minh')).date()
st.set_page_config(page_title='Quản lý khách sạn', page_icon='🏨', layout='wide')

# Thông tin kết nối Aiven MySQL
DB_USER = "avnadmin"
DB_PASSWORD = "AVNS_sQe0LzJogTMG4gdz-By"
DB_HOST = "mysql-39428747-tuetrangia950-3ce0.j.aivencloud.com"
DB_PORT = 27114
DB_NAME = "defaultdb"  # Đổi nếu database của bạn có tên khác

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
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""
    ]
    with connect() as conn:
        for sql in statements:
            conn.execute(sql)
        # Chỉ tạo 8 phòng gốc nếu cơ sở dữ liệu hoàn toàn chưa có phòng.
        if conn.execute('SELECT COUNT(*) FROM rooms').fetchone()[0] == 0:
            conn.executemany('INSERT INTO rooms(number,room_type,price,status,note) VALUES(?,?,?,?,?)',
                [(n, t, p, 'Sạch', '') for n,t,p in [
                    ('101','Standard',800000),('102','Standard',800000),
                    ('201','Superior',1100000),('202','Superior',1100000),
                    ('301','Deluxe',1500000),('302','Deluxe',1500000),
                    ('401','Suite',2500000),('501','Villa',4500000)]])

        # Nâng cấp bảng cũ mà không xóa đơn đặt phòng hiện có.
        for field in ('adults', 'children'):
            found = conn.execute(
                'SELECT COUNT(*) FROM information_schema.COLUMNS '
                'WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=? AND COLUMN_NAME=?',
                ('bookings', field)
            ).fetchone()[0]
            if not found:
                conn.execute(f'ALTER TABLE bookings ADD COLUMN {field} INT NOT NULL DEFAULT 0')

        # Bổ sung thông tin khách hàng vào bảng hiện có, không xóa dữ liệu cũ.
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

        # Đánh dấu lần bổ sung 12 phòng để không tự thêm lại phòng đã xóa.
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
    # Mỗi khách một dòng; chỉ hiện phòng có đơn chưa hủy và chưa trả.
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

# Kiểm tra kết nối thật trước khi hiển thị ứng dụng.
try:
    with connect() as _db:
        _db.execute('SELECT 1').fetchone()
    initialize()
    db_connected = True
except mysql.connector.Error as exc:
    st.error('🔴 Không thể kết nối MySQL Aiven hoặc khởi tạo database.')
    st.code(f'Mã lỗi: {exc.errno} | {exc.msg}')
    st.info('Kiểm tra tên database, tài khoản, cổng 27114 và trạng thái dịch vụ trên Aiven.')
    st.stop()

st.markdown('''<style>
.block-container{padding-top:1.3rem}
h1,h2,h3{color:#16375d}
[data-testid="stMetric"]{background:#edf3fa;border-radius:12px;padding:16px}
</style>''', unsafe_allow_html=True)

with st.sidebar:
    st.title('🏨 HOTEL MANAGER')
    menu = st.radio('Điều hướng', ['📊 Tổng quan', '🛏️ Quản lý phòng',
        '📅 Đặt phòng', '🔑 Nhận / Trả phòng', '🧹 Buồng phòng',
        '👥 Khách hàng', '💰 Doanh thu'], key='main_menu')
    st.success('🟢 Đã kết nối MySQL Aiven')
    st.caption(f'Database: {DB_NAME} | Cổng: {DB_PORT}')

_flash = st.session_state.pop('flash_message', None)
if _flash:
    st.success(_flash)

if menu == '📊 Tổng quan':
    st.title('🏨 HOTEL MANAGEMENT SYSTEM')
    if IMAGE.exists():
        st.image(str(IMAGE), caption='Hệ thống quản lý khách sạn', use_container_width=True)
    else:
        st.info('Đặt ảnh khachsan.jpg cùng thư mục với app.py để hiển thị ảnh khách sạn.')
    st.subheader('Tổng quan khách sạn')
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
    st.caption('Đơn đặt trong tương lai không tính là phòng đang có khách. Doanh thu chỉ ghi nhận sau Check-out.')
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
            # Tự chọn khách vừa được tạo sau khi chuyển trang.
            new_customer_id = st.session_state.get('booking_customer_id')
            customer_labels = list(customer_options)
            default_index = next((i for i, label in enumerate(customer_labels)
                                  if customer_options[label] == new_customer_id), 0)
            if new_customer_id is not None:
                st.info('Đã lưu khách hàng. Vui lòng chọn phòng và ngày lưu trú để hoàn tất đặt phòng.')
            with st.form('create_booking'):
                customer_label = st.selectbox('Khách hàng', customer_labels,
                                              index=default_index)
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
            st.caption('Doanh thu được ghi nhận khi xác nhận trả phòng; chưa tích hợp cổng thanh toán.')
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
            # Không hiển thị toàn bộ số CCCD trên bảng tổng hợp.
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
            citizen_id = st.text_input('Số căn cước công dân', max_chars=12,
                                       help='Nhập 12 chữ số; có thể để trống nếu chưa cung cấp.')
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
                                (name, phone.strip(), email.strip(), citizen_id or None,
                                 birth_date, address.strip())
                            ).lastrowid
                        # Chỉ chuyển trang SAU KHI transaction đã commit thành công.
                        st.session_state['booking_customer_id'] = int(new_id)
                        st.session_state['main_menu'] = '📅 Đặt phòng'
                        st.session_state['flash_message'] = 'Đã lưu khách hàng thành công.'
                        st.rerun()
                    except mysql.connector.Error as exc:
                        st.error(f'Không thể lưu khách hàng: {exc.msg}')

elif menu == '💰 Doanh thu':
    st.title('💰 Báo cáo doanh thu')
    b = bookings()
    completed = b[b.status == 'Đã trả'].copy()
    revenue = int(completed.total.sum()) if not completed.empty else 0
    a,c,d = st.columns(3)
    a.metric('Doanh thu đã ghi nhận',vnd(revenue))
    c.metric('Lượt lưu trú hoàn tất',len(completed))
    d.metric('Trung bình / lượt',vnd(revenue / len(completed) if len(completed) else 0))
    if completed.empty:
        st.info('Chưa có lượt trả phòng để thống kê.')
    else:
        completed['Tháng'] = pd.to_datetime(completed.actual_checkout).dt.strftime('%Y-%m')
        monthly = completed.groupby('Tháng',as_index=False)['total'].sum()
        st.plotly_chart(px.bar(monthly,x='Tháng',y='total',labels={'total':'Doanh thu (VNĐ)'}),
                        use_container_width=True)
        report = completed[['id','customer','room','checkin','checkout','actual_checkout','total']].copy()
        report.columns = ['Mã đặt','Khách hàng','Phòng','Ngày nhận dự kiến',
                          'Ngày trả dự kiến','Ngày trả thực tế','Doanh thu']
        table(report)
        st.download_button('📥 Xuất CSV',report.to_csv(index=False).encode('utf-8-sig'),
                           file_name='bao_cao_doanh_thu.csv',mime='text/csv')



