import os, re, io, json, sqlite3, hashlib, secrets
from datetime import datetime
from difflib import SequenceMatcher
import pandas as pd
import streamlit as st
from PIL import Image, ImageOps, ImageEnhance, ImageFilter

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(APP_DIR, "smarttest.db")
MAX_FILE_MB = 12

st.set_page_config(page_title="SmartTest | БЖБ/ТЖБ", page_icon="📝", layout="wide")
st.markdown("""
<style>
.block-container{padding-top:1.6rem;max-width:1200px}
.hero{padding:1.25rem 1.4rem;border-radius:18px;background:linear-gradient(120deg,#123c69,#2a7f8e);color:white;margin-bottom:1rem}
.hero h1{color:white;margin:0}.hero p{margin:.4rem 0 0 0;color:#eefaff}
</style>
<div class="hero"><h1>📝 SmartTest</h1><p>БЖБ / ТЖБ жауаптарын тану, дескриптор бойынша бағалау және есеп шығару</p></div>
""", unsafe_allow_html=True)

# ---------- Authentication: configure TEACHER_PASSWORD in hosting secrets/env for real use ----------
def get_password():
    try:
        return st.secrets.get("TEACHER_PASSWORD", os.getenv("TEACHER_PASSWORD", ""))
    except Exception:
        return os.getenv("TEACHER_PASSWORD", "")

if "authenticated" not in st.session_state:
    st.session_state.authenticated = False
configured_password = get_password()
if not st.session_state.authenticated:
    st.subheader("Мұғалім кабинеті")
    st.caption("Кіру үшін мұғалім құпиясөзін енгізіңіз.")
    if not configured_password:
        st.warning("Құпиясөз орнатылмаған. Алдымен жергілікті сынақ режимін пайдаланыңыз; жария сайтқа шығарғанда TEACHER_PASSWORD құпиясын міндетті түрде орнатыңыз.")
        st.info("Жергілікті сынақ үшін әдепкі құпиясөз: SmartTest-demo-2026!  Бұл құпиясөзді жария сайтта қолданбаңыз.")
        configured_password = "SmartTest-demo-2026!"
    with st.form("login_form"):
        password = st.text_input("Құпиясөз", type="password")
        submitted = st.form_submit_button("Кіру", type="primary")
    if submitted:
        if secrets.compare_digest(password, str(configured_password)):
            st.session_state.authenticated = True
            st.rerun()
        else:
            st.error("Құпиясөз дұрыс емес.")
    st.stop()

# ---------- Database ----------
def connect_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("""CREATE TABLE IF NOT EXISTS assessments (
        id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, subject TEXT, grade TEXT,
        assessment_type TEXT, term TEXT, student TEXT, total_score REAL, max_score REAL,
        status TEXT, rubric_json TEXT, answer_text TEXT, filename TEXT)""")
    conn.commit()
    return conn

conn = connect_db()

def normalize(text):
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\w\sәғқңөұүіһӘҒҚҢӨҰҮІҺ.,%-]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()

def score_answer(answer, criteria):
    """Transparent keyword-based suggestion; teacher must confirm. Not a generative AI model."""
    norm_answer = normalize(answer)
    results, total, maximum = [], 0.0, 0.0
    for c in criteria:
        points = float(c.get("points", 0))
        maximum += points
        keys = [normalize(k) for k in c.get("keywords", []) if normalize(k)]
        if not keys:
            suggested, matched, explanation = 0.0, [], "Кілт сөздер енгізілмеген — мұғалім қолмен бағалайды."
        else:
            matched = [k for k in keys if k in norm_answer]
            ratio = len(matched) / len(keys)
            # Exact keyword coverage gives a reproducible baseline; no claim of semantic understanding.
            suggested = round(points * ratio, 2)
            explanation = f"Кілт сөздердің {len(matched)}/{len(keys)} бөлігі табылды. Ұсыныс қана."
        total += suggested
        results.append({"Дескриптор": c.get("description", ""), "Кілт сөздер": ", ".join(keys),
                        "Табылған сөздер": ", ".join(matched), "Ұсынылған балл": suggested,
                        "Ең жоғары балл": points, "Түсіндірме": explanation})
    return round(total, 2), round(maximum, 2), results

def ocr_image(uploaded, engine):
    image = Image.open(uploaded).convert("RGB")
    image = ImageOps.exif_transpose(image)
    image.thumbnail((2200, 2200))
    image = ImageEnhance.Contrast(image).enhance(1.35).filter(ImageFilter.SHARPEN)
    if engine == "Tesseract (баспа мәтінге ұсынылады)":
        try:
            import pytesseract
            # kaz data may be absent; fall back to Russian/English while clearly reporting it.
            langs = "kaz+rus+eng"
            try:
                text = pytesseract.image_to_string(image, lang=langs, config="--psm 6")
            except Exception:
                text = pytesseract.image_to_string(image, lang="rus+eng", config="--psm 6")
                return text, "Қазақша kaz тілдік дерегі табылмады немесе OCR қатесі болды; rus+eng режимі қолданылды. Нәтижені мұқият түзетіңіз."
            return text, "Tesseract OCR қолданылды. Танылған мәтінді міндетті түрде тексеріңіз."
        except Exception as e:
            return "", f"Tesseract іске қосылмады: {e}. Компьютерге Tesseract OCR орнатып, каз тілдік дерегін қосыңыз."
    # Optional PaddleOCR path, deliberately labelled experimental for Kazakh handwriting.
    try:
        from paddleocr import PaddleOCR
        ocr = PaddleOCR(lang="ru", use_textline_orientation=True)
        result = ocr.predict(input=image)
        chunks = []
        for item in result or []:
            data = item.json if hasattr(item, "json") else item
            if callable(data): data = data()
            if isinstance(data, str):
                try: data = json.loads(data)
                except Exception: pass
            if isinstance(data, dict):
                res = data.get("res", data)
                texts = res.get("rec_texts", []) if isinstance(res, dict) else []
                chunks.extend([str(t) for t in texts if str(t).strip()])
        if not chunks:
            return "", "PaddleOCR мәтін қайтара алмады. Қолжазбаны қолмен енгізіңіз немесе OCR мәтінін түзетіңіз."
        return "\n".join(chunks), "PaddleOCR (орыс тілдік моделі) эксперименттік түрде қолданылды. Қазақша қолжазбадағы әріптерді қате тануы мүмкін."
    except Exception as e:
        return "", f"PaddleOCR орнатылмаған немесе іске қосылмады: {e}. Қолжазбаны қолмен енгізуге болады."

# ---------- Sidebar ----------
with st.sidebar:
    st.markdown("### Навигация")
    page = st.radio("Бөлім", ["Басты бет", "Тапсырма және дескриптор", "Жұмысты тексеру", "Нәтижелер", "Нұсқаулық"])
    st.divider()
    st.caption("SmartTest бастапқы нұсқасы")
    if st.button("Шығу"):
        st.session_state.authenticated = False
        st.rerun()

if page == "Басты бет":
    st.subheader("Мұғалімнің жұмыс үстелі")
    c1,c2,c3 = st.columns(3)
    c1.metric("Сақталған тексерулер", conn.execute("SELECT COUNT(*) FROM assessments").fetchone()[0])
    c2.metric("Оқушылар жұмысы", conn.execute("SELECT COUNT(DISTINCT student) FROM assessments").fetchone()[0])
    row = conn.execute("SELECT COALESCE(AVG(CASE WHEN max_score>0 THEN total_score/max_score*100 END),0) FROM assessments WHERE status='Бекітілді'").fetchone()
    c3.metric("Бекітілген жұмыстардың орташа пайызы", f"{row[0]:.1f}%")
    st.info("Жұмысты «Тапсырма және дескриптор» бөлімінен бастаңыз. Жүйенің баллдары — ұсыныс; мұғалім бекітпейінше қорытынды баға болып саналмайды.")
    st.markdown("**Жұмыс реті:** 1) бағалау критерийлерін енгізу → 2) фото/PDF немесе мәтінді жүктеу → 3) OCR мәтінін түзету → 4) балл ұсынысын тексеру → 5) нәтижені бекіту және Excel-ге шығару.")

elif page == "Тапсырма және дескриптор":
    st.subheader("БЖБ/ТЖБ тапсырмасын баптау")
    with st.form("assessment_setup"):
        col1,col2,col3 = st.columns(3)
        subject = col1.text_input("Пән", "Қазақ тілі")
        grade = col2.selectbox("Сынып", [str(i) for i in range(1,12)], index=5)
        assessment_type = col3.selectbox("Бағалау түрі", ["БЖБ", "ТЖБ"])
        term = st.selectbox("Тоқсан", ["1-тоқсан", "2-тоқсан", "3-тоқсан", "4-тоқсан"])
        task_title = st.text_input("Тапсырма атауы", "1-тапсырма")
        st.caption("Әр дескриптор үшін сипаттаманы, кілт сөздерді және ең жоғары балды енгізіңіз. Кілт сөздерді үтірмен бөліңіз.")
        criteria = []
        for i in range(1,6):
            with st.expander(f"Дескриптор {i}", expanded=(i==1)):
                desc = st.text_input("Дескриптор сипаттамасы", key=f"desc_{i}", placeholder="Мысалы: Негізгі ойды анықтайды")
                keys = st.text_input("Кілт сөздер", key=f"keys_{i}", placeholder="негізгі ой, автор, тақырып")
                points = st.number_input("Ең жоғары балл", min_value=0.0, max_value=20.0, value=1.0, step=0.5, key=f"points_{i}")
                criteria.append({"description": desc, "keywords": [x.strip() for x in keys.split(",") if x.strip()], "points": points})
        save = st.form_submit_button("Тапсырманы ағымдағы сессияға сақтау", type="primary")
    if save:
        criteria = [c for c in criteria if c["description"].strip() or c["keywords"]]
        if not criteria:
            st.error("Кемінде бір дескриптор енгізіңіз.")
        else:
            st.session_state.rubric = {"subject":subject,"grade":grade,"assessment_type":assessment_type,"term":term,"title":task_title,"criteria":criteria}
            st.success(f"Сақталды. Ең жоғары балл: {sum(c['points'] for c in criteria):g}")
    if "rubric" in st.session_state:
        st.markdown("#### Ағымдағы бағалау критерийлері")
        st.json(st.session_state.rubric)
        st.download_button("Критерийлерді JSON ретінде жүктеу", json.dumps(st.session_state.rubric, ensure_ascii=False, indent=2), file_name="smarttest_rubric.json", mime="application/json")
        rubric_file = st.file_uploader("Бұрын сақталған критерийлерді импорттау (JSON)", type=["json"], key="rubric_import")
        if rubric_file and st.button("JSON критерийлерін жүктеу"):
            try:
                st.session_state.rubric = json.load(rubric_file); st.success("Критерийлер жүктелді."); st.rerun()
            except Exception as e: st.error(f"JSON қатесі: {e}")

elif page == "Жұмысты тексеру":
    st.subheader("Оқушы жұмысын тексеру")
    if "rubric" not in st.session_state:
        st.warning("Алдымен «Тапсырма және дескриптор» бөлімінде критерийлерді енгізіңіз немесе JSON файл импорттаңыз.")
    else:
        rubric = st.session_state.rubric
        st.write(f"**{rubric['subject']} · {rubric['grade']}-сынып · {rubric['assessment_type']} · {rubric['term']}**")
        student = st.text_input("Оқушының аты-жөні немесе ішкі коды", placeholder="Мысалы: Оқушы-01")
        uploaded = st.file_uploader("Оқушы жұмысын жүктеу (сурет)", type=["png","jpg","jpeg","webp"], help=f"Бір файл {MAX_FILE_MB} МБ-тан аспасын.")
        engine = st.radio("Мәтінді тану тәсілі", ["Tesseract (баспа мәтінге ұсынылады)", "PaddleOCR (қолжазбаға тәжірибелік режим)"], horizontal=False)
        if uploaded:
            if uploaded.size > MAX_FILE_MB*1024*1024:
                st.error(f"Файл {MAX_FILE_MB} МБ шегінен үлкен.")
            else:
                st.image(uploaded, caption="Жүктелген жұмыс", use_container_width=True)
                if st.button("OCR арқылы мәтінді тану"):
                    with st.spinner("Мәтін танылып жатыр..."):
                        text, note = ocr_image(uploaded, engine)
                    st.session_state.ocr_text = text
                    st.session_state.ocr_note = note
                    st.session_state.upload_name = uploaded.name
        if "ocr_note" in st.session_state: st.info(st.session_state.ocr_note)
        answer_text = st.text_area("Танылған мәтінді түзетіңіз немесе жауапты осында теріңіз", value=st.session_state.get("ocr_text", ""), height=220, placeholder="Оқушы жауабын осында енгізіңіз...")
        if st.button("Жауапты бағалау", type="primary"):
            if not student.strip(): st.error("Оқушының аты-жөнін немесе кодын енгізіңіз.")
            elif not answer_text.strip(): st.error("Алдымен жауап мәтінін енгізіңіз.")
            else:
                total, maximum, detail = score_answer(answer_text, rubric["criteria"])
                st.session_state.pending_result = {"student":student.strip(),"answer":answer_text,"total":total,"maximum":maximum,"detail":detail,"rubric":rubric,"filename":st.session_state.get("upload_name", "Қолмен енгізілді")}
        if "pending_result" in st.session_state:
            result = st.session_state.pending_result
            st.markdown("### Бағалау ұсынысы")
            a,b,c = st.columns(3); a.metric("Ұсынылған балл", f"{result['total']:g}"); b.metric("Ең жоғары балл", f"{result['maximum']:g}"); c.metric("Пайыз", f"{(result['total']/result['maximum']*100 if result['maximum'] else 0):.1f}%")
            st.dataframe(pd.DataFrame(result["detail"]), use_container_width=True, hide_index=True)
            st.warning("Бұл — кілт сөздердің қамтылуына негізделген автоматты ұсыныс, толық мағыналық ЖИ бағасы емес. Оқушы жауабының мағынасын, синонимдерді және есептеу дұрыстығын мұғалім тексеруі керек.")
            with st.form("confirm_score"):
                approved_score = st.number_input("Мұғалім бекітетін қорытынды балл", min_value=0.0, max_value=float(result["maximum"]), value=float(result["total"]), step=0.5)
                teacher_note = st.text_area("Мұғалімнің ескертпесі (міндетті емес)")
                approve = st.form_submit_button("Бағаны бекітіп, сақтау", type="primary")
            if approve:
                r = result["rubric"]
                conn.execute("INSERT INTO assessments (created_at,subject,grade,assessment_type,term,student,total_score,max_score,status,rubric_json,answer_text,filename) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (datetime.now().isoformat(timespec="seconds"),r["subject"],r["grade"],r["assessment_type"],r["term"],result["student"],approved_score,result["maximum"],"Бекітілді",json.dumps({"rubric":r,"detail":result["detail"],"teacher_note":teacher_note},ensure_ascii=False),result["answer"],result["filename"]))
                conn.commit(); st.success("Мұғалім бекіткен нәтиже сақталды."); del st.session_state.pending_result

elif page == "Нәтижелер":
    st.subheader("Нәтижелер және Excel есебі")
    df = pd.read_sql_query("SELECT id AS '№', created_at AS 'Күні', subject AS 'Пән', grade AS 'Сынып', assessment_type AS 'Бағалау түрі', term AS 'Тоқсан', student AS 'Оқушы', total_score AS 'Балл', max_score AS 'Ең жоғары балл', status AS 'Мәртебе', filename AS 'Файл' FROM assessments ORDER BY id DESC", conn)
    if df.empty:
        st.info("Әзірге сақталған нәтиже жоқ.")
    else:
        col1,col2 = st.columns(2)
        grades = ["Барлығы"] + sorted(df["Сынып"].dropna().astype(str).unique().tolist())
        selected_grade = col1.selectbox("Сынып бойынша сүзу", grades)
        selected_type = col2.selectbox("Бағалау түрі", ["Барлығы","БЖБ","ТЖБ"])
        filtered = df.copy()
        if selected_grade != "Барлығы": filtered = filtered[filtered["Сынып"].astype(str)==selected_grade]
        if selected_type != "Барлығы": filtered = filtered[filtered["Бағалау түрі"]==selected_type]
        filtered["Пайыз"] = filtered.apply(lambda x: round(x["Балл"]/x["Ең жоғары балл"]*100,1) if x["Ең жоғары балл"] else 0, axis=1)
        st.dataframe(filtered, use_container_width=True, hide_index=True)
        if not filtered.empty:
            summary = filtered.groupby(["Сынып","Пән","Бағалау түрі"], dropna=False).agg(**{"Жұмыс саны": ("Оқушы", "count"), "Орташа_пайыз": ("Пайыз", "mean"), "Орташа_балл": ("Балл", "mean")}).reset_index()
            st.markdown("#### Қысқаша талдау")
            st.dataframe(summary.round(2), use_container_width=True, hide_index=True)
            output = io.BytesIO()
            with pd.ExcelWriter(output, engine="openpyxl") as writer:
                filtered.to_excel(writer, index=False, sheet_name="Нәтижелер")
                summary.to_excel(writer, index=False, sheet_name="Талдау")
            st.download_button("📥 Excel есебін жүктеу", output.getvalue(), file_name=f"SmartTest_esep_{datetime.now():%Y%m%d_%H%M}.xlsx", mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary")
            if st.checkbox("Таңдалған нәтижелерді дерекқордан жою"):
                ids = filtered["№"].astype(int).tolist()
                if st.button("Жоюды растау", type="secondary"):
                    conn.executemany("DELETE FROM assessments WHERE id=?", [(i,) for i in ids]); conn.commit(); st.success("Таңдалған жазбалар жойылды."); st.rerun()

else:
    st.subheader("Орнату және қауіпсіздік нұсқаулығы")
    st.markdown("""
### Windows компьютерінде іске қосу
1. Python 3.11 орнатыңыз.
2. Осы жоба бумасын компьютерге көшіріңіз.
3. Терминал/CMD ашып, жоба бумасына өтіңіз.
4. `pip install -r requirements.txt` пәрменін орындаңыз.
5. `streamlit run app.py` пәрменін орындаңыз.
6. Браузерде `http://localhost:8501` ашылады.

### OCR орнату
- Баспа мәтін үшін Tesseract OCR бағдарламасын бөлек орнатыңыз. Қазақша `kaz`, орысша `rus`, ағылшынша `eng` тілдік деректері қажет. `packages.txt` Linux хостингте Tesseract пакеттерін орнатуға көмектеседі.
- Қолжазба режимі эксперименттік: PaddleOCR қосымша тәуелділік ретінде орнатылады. `pip install paddleocr paddlepaddle` пәрмені құрылғыға қарай көп орын алуы мүмкін. Қазақша қолжазбаны дәл тануға кепілдік берілмейді; танылған мәтінді түзету қажет.

### Интернетке жариялау
1. Жоба файлдарын GitHub репозиторийіне салыңыз.
2. Streamlit Community Cloud-та аккаунт ашып, **Create app** таңдаңыз.
3. Репозиторийді, тармақты және `app.py` файлын көрсетіп, Deploy басыңыз.
4. App settings → Secrets бөліміне `TEACHER_PASSWORD = "ұзын-күрделі-құпиясөз"` енгізіңіз.

### Деректер қауіпсіздігі
- Бұл бастапқы нұсқада қарапайым бір құпиясөзді кіру бар; әр мұғалімге жеке аккаунт/рөлдер жүйесі әлі жасалмаған.
- Оқушының аты-жөні мен бағасы жеке дерек. Мектептің рұқсатынсыз нақты оқушы деректерін жалпыға ашық сайтқа жүктемеңіз.
- Streamlit Community Cloud-та жергілікті SQLite файлы тұрақты сақтау қоймасы ретінде кепілденбейді. Нақты мектеп жұмысына көшкенде рұқсаттары шектелген сыртқы дерекқор, резервтік көшірме, деректерді сақтау мерзімі және журналдау қажет.
- Құпиясөзді кодқа немесе GitHub-қа жазбаңыз. Нақты бағаларды мұғалім бекітсін.
""")
    st.markdown("**Ресми нұсқаулықтар:** [Streamlit жариялау](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app) · [Құпияларды сақтау](https://docs.streamlit.io/deploy/concepts/secrets) · [PaddleOCR](https://www.paddleocr.ai/)")

st.divider()
st.caption("SmartTest бастапқы нұсқасы · Деректерді сақтамас бұрын мұғалім жауап пен баллды тексеруі керек.")
