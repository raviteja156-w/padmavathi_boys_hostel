"""
seed_data.py - ONE-TIME script: removes ALL existing students and loads the data below.

Usage (run from the project folder, with DATABASE_URL set in your environment or .env):

    python seed_data.py --dry-run     # only checks the data, touches nothing
    python seed_data.py               # asks for confirmation, then replaces all students
    python seed_data.py --yes         # same, without the confirmation question
    python seed_data.py --yes --clear-payments
                                      # also empties payment_submissions + payment_history

Only the `students` table is replaced by default. Room notes, settings and payment
history are left alone unless you pass --clear-payments.

Rules followed (nothing is guessed or invented):
  * Missing contact / date of join stay blank; missing rent is stored as 0.
  * Amount Paid = 0 and Balance = Total Rent for everyone (no payment info was given).
  * Each room's "sharing" is the number shown in the register (beds in the room);
    empty beds are simply vacant.
"""
import sys
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import os

OLD, NEW = 'Old Hostel', 'New Hostel'
HALL = 11  # the Hall is stored as room_number 11 (see app.py)

# (name, date_of_join 'DD/MM/YYYY' or None, contact or None, total_rent or None, note)
# Every room: (hostel, room_number, sharing, [students])
DATA = [
    # ------------------------------ OLD HOSTEL ------------------------------
    (OLD, 1, 4, [  # Bhukya Harshith removed -> that bed is empty
        ('G Mallikarjun Reddy', '15/03/2026', '7989305965', 7000, ''),
        ('P Rahul',             '11/08/2025', '6305710085', 7000, ''),
        ('Poorna chandh',       '05/07/2026', '9010546585', 7000, ''),
    ]),
    (OLD, 2, 3, [
        ('Pranav',               None,         None,         None, ''),
        ('S Harshit Sai Chandh', '01/06/2026', '6304064393', 7200, ''),
        ('S Narasimha Babu',     '07/12/2025', '9666020695', 7500, ''),
    ]),
    (OLD, 3, 6, [
        ('G Vijaya Bhaskar', '18/08/2026', '2328048145', 6000, ''),
        ('S Sai Kumar',      '01/05/2025', '9492553706', 5500, ''),
        ('Raj Kumar',        '01/03/2025', '9652836410', 6000, ''),
        ('Shaik Ameer',      '15/04/2026', '9391598918', 5500, ''),
        ('Arun',             '16/08/2026', '8074410714', 6000, ''),
        ('B.Ram charan',     '10/03/2026', '9121534983', 6000, ''),
    ]),
    (OLD, 4, 4, [
        ('Anjani Kumar Takoor', '10/03/2026', '8340167738', 7000, ''),
        ('J Rishikesh',         '03/07/2026', '6301152050', 7000, ''),
        ('Ch Srikanth',         '24/04/2026', '8309184011', 7000, ''),
        ('Shunith',             '19/07/2026', '7993490022', 6500, ''),
    ]),
    (OLD, 5, 5, [
        ('Y.Prahaladh',    '10/07/2026', '7620355088', 5500, ''),
        ('D Rahul Netha',  '01/06/2026', '6300085149', 6000, ''),
        ('K Chandhu',      '04/06/2026', '9059430091', 6000, ''),
        ('K Sravan Kumar', '01/04/2025', '8686504910', 6000, ''),
        ('K Siddhartha L', '14/09/2025', '9347586414', 6000, ''),
    ]),
    (OLD, 6, 4, [
        ('M Ajay',       '22/06/2026', '7036692116', 6000, ''),
        ('Rohith Kumar', '03/08/2025', '7993688314', 7000, ''),
        ('MD Azhar',     '06/08/2026', None,         7000, ''),   # contact intentionally blank
        ('K Madhav',     '22/08/2026', '967670369',  7000, ''),
    ]),
    (OLD, 7, 5, [
        ('Hariharan',       '04/06/2026', '7032414481', 6000, ''),
        ('K Manohar',       '08/06/2025', '7997587213', 6000, ''),
        ('P Naveen',        '26/05/2026', '7981815278', 5700, ''),
        ('S Hemanth Reddy', '16/04/2026', '9951328309', 6000, ''),
        ('T Hanumanth',     '03/08/2026', '6309767194', 6000, ''),
    ]),
    (OLD, 8, 5, [
        ('B Shiva Chaitanya', '05/07/2026', '6301649775', 5500, ''),
        ('M Vishnu',          '07/08/2026', '7396621813', 6000, ''),
        ('N Ajay Goud',       '14/05/2025', '9701085865', 5500, ''),
        ('Anil',              '13/09/2026', '8143929542', 6000, ''),
        ('B.Srinivas',        '18/09/2026', '9381269200', 6000, ''),
    ]),
    (OLD, 9, 5, [
        ('L Sai Tharun Reddy',    '06/05/2025', '7981454340',  6000, ''),
        ('Nitesh',                '20/08/2025', '6372725657',  6000, ''),
        ('R Gowtham',             '01/05/2025', '9688361777',  6000, ''),
        ('Venkata Chandra Mouli', '08/04/2025', '63000823875', 6000, ''),
        ('J Teja',                '01/07/2025', '9392507574',  6000, ''),
    ]),
    (OLD, 10, 1, [
        ('Souraj Bhopuri', '26/05/2025', '7002318322', 12000, ''),
    ]),

    # ------------------------------ NEW HOSTEL ------------------------------
    (NEW, 1, 2, [
        ('V.Nikhil', '01/08/2026', '9912581974', 8000, ''),
        ('B.Manish', '01/09/2025', '7013988022', 8000, ''),
    ]),
    (NEW, 2, 3, [  # one bed empty
        ('Avishkar',    '14/08/2025', '9108066575', 7500, ''),
        ('Manoj Kumar', '01/02/2026', '8523872937', 5500, ''),
    ]),
    (NEW, 3, 5, [
        ('Sandeep',                '12/03/2025', '8106839152', 6000, ''),
        ('Hari kumar',             '01/05/2025', '6281691253', 6000, ''),
        ('Shiva',                  '18/08/2025', '8247110266', 6000, ''),
        ('Jaswa',                  '12/08/2025', '7673907642', 6000, ''),
        ('Sathya Surya narasinha', '01/04/2025', '8367562566', 6000, ''),
    ]),
    (NEW, 4, 5, [
        ('Manthani sanjay', '06/06/2026', '9059359650', 6000, ''),
        ('Soujith kumar',   '02/06/2026', '8247069771', 6000, ''),
        ('Pavan Kumar',     '16/06/2026', '8978666095', 5000, ''),
        ('B Raju kumar',    '23/06/2026', '8639759087', 6000, 'empty for month'),
        ('Ramakrishna A',   '02/06/2026', '9989141771', 6000, ''),
    ]),
    (NEW, 5, 4, [  # one bed empty
        ('Venkata murali Ganesh', '01/06/2026', '9573407723', 6000, ''),
        ('Dinesh sai teja',       '10/06/2026', '9030637321', 6000, ''),
        ('Pranay Jeevan',         '05/01/2026', '6309167660', 5500, ''),
    ]),
    (NEW, 6, 3, [
        ('Mohammed Imran', '15/07/2026', '7981246336', 7000, ''),
        ('Yasin',          '31/08/2026', '8688332744', 7500, ''),
        ('Sri Vaishnav',   '15/07/2026', '8374567158', 7000, ''),
    ]),
    (NEW, 7, 1, [
        ('Sheshi Kumar', '29/04/2026', '8602370934', 12000, ''),
    ]),
    (NEW, 8, 5, [  # Pavan.S moved here from the Hall
        ('Srikanth L.',    '04/06/2026', '6302460696', 6000, ''),
        ('Mano srikarana', '03/06/2026', '9346494564', 6000, ''),
        ('Jaswanth',       '14/10/2025', '9985460625', 4000, ''),
        ('Thirupathi',     None,         None,         None, 'Only name provided; date, contact and rent not given'),
        ('Pavan.S',        '30/08/2026', '9949312883', 6000, ''),
    ]),
    (NEW, 9, 3, [
        ('Rama krishna P', '15/07/2026', '7993950197', 7000, ''),
        ('Akshay',         '17/07/2026', '9177210431', 7000, ''),
        ('Rama Charan',    '17/07/2026', '9391835257', 7000, ''),
    ]),
    (NEW, 10, 4, [
        ('Vardhan',         '03/10/2025', '7386533123', 7000, ''),
        ('Akshaj',          '03/06/2026', '9347746220', 4000, ''),
        ('Veera chaitanya', '11/08/2026', '9642985818', 7000, ''),
        ('Srinadh',         '15/08/2026', '6301115283', 7000, ''),
    ]),
    (NEW, HALL, 9, [  # Pavan.S moved out; Ch. Bhanu rent set to 5500
        ('T. Shushanth',    '31/08/2026', '7730901374',       5500, ''),
        ('B. Manohar',      '31/08/2026', '9912234385',       5000, ''),
        ('K. Akshay Kumar', '31/08/2026', '6304073722',       5500, ''),
        ('Ch. Bhanu',       '31/08/2026', '8688380853',       5500, ''),
        ('Deeleep',         '05/09/2026', '9014154804',       5500, ''),
        ('E. Vikram',       '08/09/2026', '85555808 / 628?',  5500, ''),
        ('T. Anji',         '01/07/2026', '984972?760',       5000, ''),
        ('Akash',           '16/09/2026', '9773088118',       5500, ''),
        ('Mani Charan Tej', '16/09/2026', '9391171749',       5500, ''),
    ]),
]


def iso(d):
    """'DD/MM/YYYY' -> 'YYYY-MM-DD' (the format the app stores); None stays None."""
    return datetime.strptime(d, '%d/%m/%Y').strftime('%Y-%m-%d') if d else None


def build_rows():
    rows = []
    for hostel, room, sharing, students in DATA:
        assert 1 <= sharing <= 9, (hostel, room, sharing)
        assert len(students) <= sharing, f'{hostel} room {room}: {len(students)} people > {sharing} beds'
        for name, doj, contact, rent, note in students:
            rent = float(rent or 0)
            rows.append((hostel, room, sharing, name, contact or '', rent, 0.0, rent,
                         note, iso(doj)))
    return rows


def main():
    args = set(sys.argv[1:])
    rows = build_rows()
    old_n = sum(1 for r in rows if r[0] == OLD)
    new_n = sum(1 for r in rows if r[0] == NEW)
    print(f'Data check OK: {len(rows)} students  (Old Hostel {old_n}, New Hostel {new_n})')
    if '--dry-run' in args:
        print('Dry run only - database was not touched.')
        return

    url = os.environ.get('DATABASE_URL')
    if not url:
        sys.exit('DATABASE_URL is not set (put it in your environment or .env).')
    if '--yes' not in args:
        ans = input('This DELETES ALL existing students and loads the new data. Type YES to continue: ')
        if ans.strip() != 'YES':
            sys.exit('Cancelled - nothing was changed.')

    import psycopg2
    conn = psycopg2.connect(url)
    try:
        cur = conn.cursor()
        # Allow up to 9-sharing (same change app.py makes at startup).
        cur.execute('ALTER TABLE students DROP CONSTRAINT IF EXISTS students_sharing_check')
        cur.execute('ALTER TABLE students ADD CONSTRAINT students_sharing_check CHECK (sharing BETWEEN 1 AND 9)')
        cur.execute('DELETE FROM students')
        if '--clear-payments' in args:
            cur.execute('DELETE FROM payment_submissions')
            cur.execute('DELETE FROM payment_history')
        now = datetime.utcnow().isoformat()
        for r in rows:
            cur.execute('''
                INSERT INTO students
                (hostel, room_number, sharing, name, contact, total_rent, amount_paid, balance,
                 note, date_of_join, created_at, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ''', r + (now, now))
        conn.commit()
        print(f'Done. Old students removed; {len(rows)} students loaded.')
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    main()
