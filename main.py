from flask import Flask, redirect
import random

app = Flask(__name__)

@app.route("/")
def index():
    urls = [
        "https://www.surveycake.com/s/XGYq0",
        "https://www.surveycake.com/s/MoeBD",
        "https://www.surveycake.com/s/BbGBV",
        "https://www.surveycake.com/s/1MGw1"
    ]
    return redirect(random.choice(urls), code=302)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
