import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

BASE = Path(__file__).resolve().parent
DB = BASE / 'hotel.db'
IMAGE = BASE / 'khachsan.jpg'
TYPES = {'Standard': 800_000, 'Superior': 1_100_000, 'Deluxe': 1_500_000,
         'Suite': 2_500_000, 'Villa': 4_500_000}
HOUSEKEEPING = ['Sạch', 'Bẩn', 'Đang vệ sinh', 'Bảo trì']

st.set_page_config(page_title='Quản lý khách sạn', page_icon='🏨', layout='wide')

@contextmanager
def connect():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def read(sql, params=()):
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)

def write(sql, params=()):
    with connect() as conn:
        return conn.execute(sql, params).lastrowid

def initialize():
    with connect() as conn:
        conn.executescript('''
        CREATE TABLE IF NOT EXISTS rooms (
            id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL,
            room_type TEXT NOT NULL, price INTEGER NOT NULL CHECK(price>=0),
            status TEXT NOT NULL DEFAULT 'Sạch', note TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS customers (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT NOT NULL DEFAULT '',
            email TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id),
            room_id INTEGER NOT NULL REFERENCES rooms(id),
            checkin TEXT NOT NULL, checkout TEXT NOT NULL,
            actual_checkout TEXT, status TEXT NOT NULL DEFAULT 'Đã đặt',
            total INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
            CHECK(checkout > checkin));
        ''')
        if conn.execute('SELECT COUNT(*) FROM rooms').fetchone()[0] == 0:
            conn.executemany('INSERT INTO rooms(number,room_type,price) VALUES(?,?,?)',
                [('101','Standard',800000), ('102','Standard',800000),
                 ('201','Superior',1100000), ('202','Superior',1100000),
                 ('301','Deluxe',1500000), ('302','Deluxe',1500000),
                 ('401','Suite',2500000), ('501','Villa',4500000)])

def rooms():
    return read('''SELECT r.*, CASE WHEN EXISTS
        (SELECT 1 FROM bookings b WHERE b.room_id=r.id AND b.status='Đang ở')
        THEN 'Có khách' ELSE r.status END AS display_status
        FROM rooms r ORDER BY r.number''')

def customers():
    return read('SELECT * FROM customers ORDER BY id DESC')

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
    st.success(text)
    st.rerun()

initialize()
st.markdown('''<style>
.block-container{padding-top:1.3rem}
h1,h2,h3{color:#16375d}
[data-testid="stMetric"]{background:#edf3fa;border-radius:12px;padding:16px}
</style>''', unsafe_allow_html=True)

with st.sidebar:
    st.title('🏨 HOTEL MANAGER')
    menu = st.radio('Điều hướng', ['📊 Tổng quan', '🛏️ Quản lý phòng',
        '📅 Đặt phòng', '🔑 Nhận / Trả phòng', '🧹 Buồng phòng',
        '👥 Khách hàng', '💰 Doanh thu'])
    st.caption('Phiên bản thực hành • Dữ liệu lưu trên máy')

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
    revenue = int(b.loc[b.status == 'Đã trả', 'total'].sum()) if not b.empty else 0
    a, c, d, e = st.columns(4)
    a.metric('Tổng số phòng', len(r))
    c.metric('Đang có khách', occupied)
    d.metric('Phòng sạch chưa có khách', clean)
    e.metric('Doanh thu ghi nhận', vnd(revenue))
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
                    except sqlite3.IntegrityError:
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
                        except sqlite3.IntegrityError:
                            st.error('Số phòng đã tồn tại.')
            confirm = st.checkbox('Xác nhận xóa phòng này')
            if st.button('Xóa phòng', disabled=not confirm):
                with connect() as conn:
                    used = conn.execute('SELECT COUNT(*) FROM bookings WHERE room_id=?',(room_id,)).fetchone()[0]
                    if used:
                        st.error('Không thể xóa phòng đã có lịch sử đặt phòng.')
                    else:
                        conn.execute('DELETE FROM rooms WHERE id=?',(room_id,))
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
            with st.form('create_booking'):
                customer_label = st.selectbox('Khách hàng', list(customer_options))
                start = st.date_input('Ngày nhận', date.today())
                end = st.date_input('Ngày trả', date.today()+timedelta(days=1))
                room_label = st.selectbox('Phòng', list(room_options)) if room_options else None
                note = st.text_area('Yêu cầu đặc biệt')
                if st.form_submit_button('Xác nhận đặt', type='primary', disabled=not room_options):
                    room_id = room_options[room_label]
                    if start < date.today() or end <= start:
                        st.error('Ngày nhận không được trong quá khứ và ngày trả phải sau ngày nhận.')
                    else:
                        with connect() as conn:
                            current = conn.execute('SELECT * FROM rooms WHERE id=?',(room_id,)).fetchone()
                            if current['status'] == 'Bảo trì' or booking_conflict(conn,room_id,start,end):
                                st.error('Phòng bảo trì hoặc đã có lịch đặt trùng thời gian.')
                            else:
                                total = (end-start).days * current['price']
                                conn.execute('''INSERT INTO bookings
                                    (customer_id,room_id,checkin,checkout,total,note)
                                    VALUES(?,?,?,?,?,?)''',
                                    (customer_options[customer_label],room_id,str(start),str(end),total,note))
                                st.success(f'Đặt phòng thành công. Dự kiến: {vnd(total)}')
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
                    room = conn.execute('SELECT status FROM rooms WHERE id=?',(int(item.room_id),)).fetchone()
                    occupied = conn.execute("SELECT COUNT(*) FROM bookings WHERE room_id=? AND status='Đang ở'",
                                            (int(item.room_id),)).fetchone()[0]
                    if room['status'] != 'Sạch' or occupied:
                        st.error('Phòng chưa sạch hoặc đang có khách.')
                    elif not (date.fromisoformat(item.checkin) <= date.today() < date.fromisoformat(item.checkout)):
                        st.error('Chỉ nhận phòng trong khoảng ngày đặt. Hãy điều chỉnh đặt phòng nếu cần.')
                    else:
                        conn.execute("UPDATE bookings SET status='Đang ở' WHERE id=? AND status='Đã đặt'",(bid,))
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
                                 (str(date.today()),total,bid))
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
                    message_and_reload('Đã cập nhật tình trạng phòng.')

elif menu == '👥 Khách hàng':
    st.title('👥 Khách hàng')
    tab1, tab2 = st.tabs(['Danh sách', 'Thêm khách hàng'])
    with tab1:
        c = customers()
        keyword = st.text_input('Tìm tên hoặc số điện thoại').strip()
        if keyword:
            c = c[c.name.str.contains(keyword,case=False,regex=False,na=False) |
                  c.phone.str.contains(keyword,case=False,regex=False,na=False)]
        table(c)
    with tab2:
        with st.form('new_customer'):
            name = st.text_input('Họ tên *')
            phone = st.text_input('Số điện thoại')
            email = st.text_input('Email')
            if st.form_submit_button('Thêm khách hàng',type='primary'):
                if not name.strip():
                    st.error('Vui lòng nhập họ tên.')
                else:
                    write('INSERT INTO customers(name,phone,email) VALUES(?,?,?)',
                          (name.strip(),phone.strip(),email.strip()))
                    message_and_reload('Đã thêm khách hàng.')

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


