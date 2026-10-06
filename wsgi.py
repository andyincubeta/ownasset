from app import create_app

app = create_app(start_scheduler=True)

if __name__ == '__main__':
    app.run()