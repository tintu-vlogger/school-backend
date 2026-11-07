"""Library defaults for every institution (borrowing rules, categories) and the demo library:
150 titles with copies, loans (some overdue), a few fines and holds. Aarav Kumar (the demo
student) has one book on loan."""
import random
from datetime import timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

BOOKS = [
    # title, authors, category, year, kind
    ("Malgudi Days", "R. K. Narayan", "Fiction", 1943), ("Swami and Friends", "R. K. Narayan", "Fiction", 1935),
    ("The Guide", "R. K. Narayan", "Fiction", 1958), ("Wings of Fire", "A. P. J. Abdul Kalam", "Biography", 1999),
    ("Ignited Minds", "A. P. J. Abdul Kalam", "Non-fiction", 2002), ("The Story of My Experiments with Truth", "M. K. Gandhi", "Biography", 1927),
    ("Discovery of India", "Jawaharlal Nehru", "History", 1946), ("Letters from a Father to His Daughter", "Jawaharlal Nehru", "History", 1929),
    ("Gitanjali", "Rabindranath Tagore", "Languages", 1910), ("The Blue Umbrella", "Ruskin Bond", "Children", 1980),
    ("The Room on the Roof", "Ruskin Bond", "Fiction", 1956), ("Rusty Runs Away", "Ruskin Bond", "Children", 1993),
    ("Panchatantra Tales", "Vishnu Sharma", "Children", 1990), ("Tales of Tenali Raman", "Traditional", "Children", 2005),
    ("Akbar and Birbal", "Traditional", "Children", 2004), ("Grandma's Bag of Stories", "Sudha Murty", "Children", 2012),
    ("How I Taught My Grandmother to Read", "Sudha Murty", "Fiction", 2004), ("Wise and Otherwise", "Sudha Murty", "Non-fiction", 2002),
    ("The Magic Drum and Other Favourite Stories", "Sudha Murty", "Children", 2006), ("Harry Potter and the Philosopher's Stone", "J. K. Rowling", "Fiction", 1997),
    ("Harry Potter and the Chamber of Secrets", "J. K. Rowling", "Fiction", 1998), ("Harry Potter and the Prisoner of Azkaban", "J. K. Rowling", "Fiction", 1999),
    ("The Hobbit", "J. R. R. Tolkien", "Fiction", 1937), ("Charlotte's Web", "E. B. White", "Children", 1952),
    ("Matilda", "Roald Dahl", "Children", 1988), ("Charlie and the Chocolate Factory", "Roald Dahl", "Children", 1964),
    ("The BFG", "Roald Dahl", "Children", 1982), ("Wonder", "R. J. Palacio", "Fiction", 2012),
    ("The Diary of a Young Girl", "Anne Frank", "Biography", 1947), ("To Kill a Mockingbird", "Harper Lee", "Fiction", 1960),
    ("The Little Prince", "Antoine de Saint-Exupery", "Fiction", 1943), ("Treasure Island", "Robert Louis Stevenson", "Fiction", 1883),
    ("Robinson Crusoe", "Daniel Defoe", "Fiction", 1719), ("Gulliver's Travels", "Jonathan Swift", "Fiction", 1726),
    ("Alice's Adventures in Wonderland", "Lewis Carroll", "Children", 1865), ("The Jungle Book", "Rudyard Kipling", "Children", 1894),
    ("Black Beauty", "Anna Sewell", "Children", 1877), ("Little Women", "Louisa May Alcott", "Fiction", 1868),
    ("Oliver Twist", "Charles Dickens", "Fiction", 1838), ("A Christmas Carol", "Charles Dickens", "Fiction", 1843),
    ("Great Expectations", "Charles Dickens", "Fiction", 1861), ("Pride and Prejudice", "Jane Austen", "Fiction", 1813),
    ("The Adventures of Tom Sawyer", "Mark Twain", "Fiction", 1876), ("Around the World in Eighty Days", "Jules Verne", "Fiction", 1872),
    ("Twenty Thousand Leagues Under the Sea", "Jules Verne", "Fiction", 1870), ("The Secret Garden", "Frances Hodgson Burnett", "Children", 1911),
    ("Heidi", "Johanna Spyri", "Children", 1881), ("Anne of Green Gables", "L. M. Montgomery", "Fiction", 1908),
    ("The Wind in the Willows", "Kenneth Grahame", "Children", 1908), ("Animal Farm", "George Orwell", "Fiction", 1945),
    ("The Old Man and the Sea", "Ernest Hemingway", "Fiction", 1952), ("Lord of the Flies", "William Golding", "Fiction", 1954),
    ("The Alchemist", "Paulo Coelho", "Fiction", 1988), ("A Brief History of Time", "Stephen Hawking", "Science", 1988),
    ("Cosmos", "Carl Sagan", "Science", 1980), ("The Selfish Gene", "Richard Dawkins", "Science", 1976),
    ("Surely You're Joking, Mr. Feynman!", "Richard P. Feynman", "Biography", 1985), ("The Man Who Knew Infinity", "Robert Kanigel", "Biography", 1991),
    ("Sapiens: A Brief History of Humankind", "Yuval Noah Harari", "History", 2011), ("Guns, Germs, and Steel", "Jared Diamond", "History", 1997),
    ("India After Gandhi", "Ramachandra Guha", "History", 2007), ("The Wonder That Was India", "A. L. Basham", "History", 1954),
    ("Freedom at Midnight", "Larry Collins, Dominique Lapierre", "History", 1975), ("Train to Pakistan", "Khushwant Singh", "Fiction", 1956),
    ("The God of Small Things", "Arundhati Roy", "Fiction", 1997), ("Midnight's Children", "Salman Rushdie", "Fiction", 1981),
    ("A Suitable Boy", "Vikram Seth", "Fiction", 1993), ("The White Tiger", "Aravind Adiga", "Fiction", 2008),
    ("Godan", "Munshi Premchand", "Languages", 1936), ("Idgah and Other Stories", "Munshi Premchand", "Languages", 1933),
    ("Chandrakanta", "Devaki Nandan Khatri", "Languages", 1888), ("Madhushala", "Harivansh Rai Bachchan", "Languages", 1935),
    ("Mathematical Puzzles and Diversions", "Martin Gardner", "Mathematics", 1959), ("The Joy of x", "Steven Strogatz", "Mathematics", 2012),
    ("Fermat's Last Theorem", "Simon Singh", "Mathematics", 1997), ("The Number Devil", "Hans Magnus Enzensberger", "Mathematics", 1997),
    ("How to Solve It", "George Polya", "Mathematics", 1945), ("Vedic Mathematics", "Bharati Krishna Tirtha", "Mathematics", 1965),
    ("Python Crash Course", "Eric Matthes", "Computers", 2019), ("Scratch Programming for Kids", "Majed Marji", "Computers", 2014),
    ("Code: The Hidden Language", "Charles Petzold", "Computers", 1999), ("Hello World: How to Be Human in the Age of the Machine", "Hannah Fry", "Computers", 2018),
    ("Computer Science Unplugged", "Tim Bell, Ian Witten", "Computers", 2015), ("Introduction to Algorithms", "Cormen, Leiserson, Rivest, Stein", "Computers", 2009),
    ("Concepts of Physics Vol. 1", "H. C. Verma", "Science", 1992), ("Concepts of Physics Vol. 2", "H. C. Verma", "Science", 1992),
    ("Organic Chemistry", "Morrison, Boyd", "Science", 1992), ("Biology: A Global Approach", "Campbell, Reece", "Science", 2017),
    ("The Gene: An Intimate History", "Siddhartha Mukherjee", "Science", 2016), ("The Emperor of All Maladies", "Siddhartha Mukherjee", "Science", 2010),
    ("Silent Spring", "Rachel Carson", "Science", 1962), ("The Origin of Species", "Charles Darwin", "Science", 1859),
    ("Geography of India", "Majid Husain", "Geography", 2014), ("Oxford School Atlas", "Oxford University Press", "Reference", 2022),
    ("Physical Geography", "Savindra Singh", "Geography", 2016), ("The Great Railway Bazaar", "Paul Theroux", "Geography", 1975),
    ("Chasing the Monsoon", "Alexander Frater", "Geography", 1990), ("Oxford Advanced Learner's Dictionary", "A. S. Hornby", "Reference", 2020),
    ("Wren and Martin High School English Grammar", "P. C. Wren, H. Martin", "Languages", 1935), ("Roget's Thesaurus", "Peter Mark Roget", "Reference", 1852),
    ("Encyclopaedia Britannica Concise", "Britannica", "Reference", 2008), ("Manorama Year Book", "Malayala Manorama", "Reference", 2026),
    ("Guinness World Records", "Guinness", "Reference", 2026), ("Children's Illustrated Encyclopedia", "DK", "Reference", 2019),
    ("The Story of Art", "E. H. Gombrich", "Arts", 1950), ("Drawing on the Right Side of the Brain", "Betty Edwards", "Arts", 1979),
    ("Indian Classical Dance", "Leela Venkataraman", "Arts", 2002), ("A Little History of the World", "E. H. Gombrich", "History", 1936),
    ("Raja Ravi Varma: Portrait of an Artist", "Rupika Chawla", "Arts", 2010), ("The Music Room", "Namita Devidayal", "Arts", 2007),
    ("Atomic Habits", "James Clear", "Non-fiction", 2018), ("The 7 Habits of Highly Effective Teens", "Sean Covey", "Non-fiction", 1998),
    ("Mindset", "Carol S. Dweck", "Non-fiction", 2006), ("You Can Win", "Shiv Khera", "Non-fiction", 1998),
    ("Playing It My Way", "Sachin Tendulkar", "Biography", 2014), ("Unbreakable", "M. C. Mary Kom", "Biography", 2013),
    ("The Race of My Life", "Milkha Singh", "Biography", 2013), ("Long Walk to Freedom", "Nelson Mandela", "Biography", 1994),
    ("I Am Malala", "Malala Yousafzai", "Biography", 2013), ("Steve Jobs", "Walter Isaacson", "Biography", 2011),
]
TEXTBOOKS = [("Mathematics", "Mathematics"), ("Science", "Science"), ("English: Honeysuckle", "Languages"),
             ("Social Science: Our Pasts", "History"), ("Hindi: Vasant", "Languages")]
SHELVES = {"Fiction": "F", "Children": "C", "Biography": "B", "History": "H", "Science": "S", "Mathematics": "M",
           "Languages": "L", "Reference": "R", "Geography": "G", "Computers": "CS", "Arts": "A", "Non-fiction": "N"}
PUBLISHERS = ["Penguin India", "Rupa Publications", "HarperCollins", "Scholastic India", "Oxford University Press",
              "Puffin", "Bloomsbury", "Orient BlackSwan", "Pan Macmillan", "NCERT"]


def setup_company(db: Session, company_id: int) -> None:
    try:
        from modules.library import service
        with db.begin_nested():
            service.ensure_defaults(db, company_id)
    except Exception:  # defaults must never block start-up or sign-up
        pass


def seed_demo(db: Session, company_id: int) -> None:
    from modules.library import service
    from modules.library.models import (LibraryBook, LibraryCategory, LibraryEResource, LibraryFine, LibraryLoan,
                                        LibraryReservation)
    from modules.students.models import Student
    if db.query(LibraryBook.id).filter(LibraryBook.company_id == company_id).first():
        return
    _foundation(db, company_id)
    if not db.query(Student.id).filter(Student.company_id == company_id).first():
        return
    service.ensure_defaults(db, company_id)
    rng = random.Random(11)
    cats = {c.name: c for c in db.query(LibraryCategory).filter(LibraryCategory.company_id == company_id).all()}
    rows = list(BOOKS)
    for grade in range(6, 12):
        for name, cat in TEXTBOOKS:
            if len(rows) >= 150:
                break
            rows.append((f"NCERT {name} - Class {grade}", "NCERT", cat, 2023))
    rows = rows[:150]
    books = []
    for i, (title, authors, cat, year) in enumerate(rows):
        shelf = f"{SHELVES.get(cat, 'X')}-{(i % 6) + 1}"
        ref = cat == "Reference"
        b = LibraryBook(company_id=company_id, title=title, authors=authors, category_id=cats[cat].id if cat in cats else None,
                        year=year, language="Hindi" if cat == "Languages" and authors in ("Munshi Premchand", "Devaki Nandan Khatri", "Harivansh Rai Bachchan") else "English",
                        publisher="NCERT" if authors == "NCERT" else rng.choice(PUBLISHERS[:-1]), shelf=shelf,
                        kind="reference" if ref else "book", reference_only=ref,
                        price=Decimal(rng.choice([199, 250, 299, 350, 399, 450, 499, 599, 799])),
                        isbn=f"978{rng.randint(1000000000, 9999999999)}", subject=cat)
        db.add(b)
        db.flush()
        service.add_copies(db, company_id, b, 1 if ref else rng.choice([1, 2, 2, 3, 3, 4]),
                           acquired_on=service.today(db, company_id) - timedelta(days=rng.randint(60, 1500)),
                           source=rng.choice(["Purchase", "Purchase", "Donation"]), condition="good")
        books.append(b)
    service.sync_members(db, company_id)
    on = service.today(db, company_id)

    from modules.library.models import LibraryCopy, LibraryMember
    members = db.query(LibraryMember).filter(LibraryMember.company_id == company_id, LibraryMember.active.is_(True)).all()
    students = [m for m in members if m.member_type == "student"]
    staff = [m for m in members if m.member_type == "staff"]
    aarav = (db.query(LibraryMember).join(Student, Student.id == LibraryMember.student_id)
             .filter(LibraryMember.company_id == company_id, Student.email == "student@test.com").first())

    def free_copy(book):
        return db.query(LibraryCopy).filter(LibraryCopy.book_id == book.id, LibraryCopy.status == "available").first()

    lendable = [b for b in books if not b.reference_only]
    # History: returned loans over the past months (drives "most borrowed").
    for _ in range(220):
        m = rng.choice(students + staff[:5])
        b = rng.choice(lendable[:60] if rng.random() < 0.7 else lendable)
        c = free_copy(b)
        if c is None:
            continue
        issued = on - timedelta(days=rng.randint(20, 300))
        db.add(LibraryLoan(company_id=company_id, copy_id=c.id, book_id=b.id, member_id=m.id, issued_on=issued,
                           due_on=issued + timedelta(days=14), returned_on=issued + timedelta(days=rng.randint(3, 18)),
                           status="returned", issued_by="Demo data", returned_by="Demo data"))
    db.flush()
    # Open loans now: most on time, some overdue.
    if aarav is not None:
        b = next(b for b in lendable if b.title == "Wings of Fire")
        service.issue(db, company_id, aarav, free_copy(b), on - timedelta(days=6))
    picks = rng.sample(students, min(40, len(students)))
    for k, m in enumerate(picks):
        if aarav is not None and m.id == aarav.id:
            continue
        b = rng.choice(lendable)
        c = free_copy(b)
        if c is None:
            continue
        days_ago = rng.randint(18, 35) if k % 4 == 0 else rng.randint(1, 12)
        try:
            service.issue(db, company_id, m, c, on - timedelta(days=days_ago))
        except Exception:
            continue
    for m in staff[:4]:
        b = rng.choice(lendable)
        c = free_copy(b)
        if c is not None:
            service.issue(db, company_id, m, c, on - timedelta(days=rng.randint(2, 20)))
    # A few fines from earlier late returns.
    returned = db.query(LibraryLoan).filter(LibraryLoan.company_id == company_id, LibraryLoan.status == "returned").limit(12).all()
    for i, l in enumerate(returned):
        late = (l.returned_on - l.due_on).days
        if late > 0:
            db.add(LibraryFine(company_id=company_id, member_id=l.member_id, loan_id=l.id, kind="overdue",
                               amount=Decimal(late), days=late, status="pending" if i % 3 else "paid",
                               settled_on=l.returned_on if i % 3 == 0 else None))
    # Holds on popular titles that are fully out.
    for b in lendable[:40]:
        if free_copy(b) is None and rng.random() < 0.4:
            m = rng.choice(students)
            if not db.query(LibraryReservation.id).filter(LibraryReservation.member_id == m.id,
                                                          LibraryReservation.book_id == b.id).first():
                try:
                    service.reserve(db, company_id, m, b, on)
                except Exception:
                    pass
    for title, url, kind, subject in [
        ("National Digital Library of India", "https://ndl.iitkgp.ac.in", "database", "All subjects"),
        ("ePathshala (NCERT e-books)", "https://epathshala.nic.in", "ebook", "Textbooks"),
        ("DIKSHA", "https://diksha.gov.in", "website", "Lessons and practice"),
        ("Project Gutenberg", "https://www.gutenberg.org", "ebook", "Classic literature"),
        ("Khan Academy", "https://www.khanacademy.org", "video", "Mathematics and science"),
    ]:
        db.add(LibraryEResource(company_id=company_id, title=title, url=url, kind=kind, subject=subject))
    db.flush()


def _foundation(db: Session, company_id: int) -> None:
    """Seeds run in module-name order, so make sure classes and students exist first."""
    from modules.academics.seed import seed_demo as academics_demo
    from modules.students.seed import seed_demo as students_demo
    academics_demo(db, company_id)
    students_demo(db, company_id)
