from flask import Flask, redirect
import random

app = Flask(__name__)

@app.route("/")
def index():
    urls = [
        "https://www.surveycake.com/s/2lk7l",
        "https://www.surveycake.com/s/XGmXr",
        "https://www.surveycake.com/s/qG4PL",
        "https://www.surveycake.com/s/bGe8v"
    ]
    return redirect(random.choice(urls), code=302)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
