from app import  *
if __name__ == '__main__':
    os.makedirs(PAGES_DIR, exist_ok=True)
    init_db()
    app.run(host='::', port=8000, debug=True)