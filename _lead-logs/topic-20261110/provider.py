import json, sys, urllib.request
base = "http://127.0.0.1:8799"
def post(path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "reset":
        print(post("/__reset", {}))
    elif mode == "script":
        steps = json.loads(open(sys.argv[2], encoding="utf-8").read()) if sys.argv[2].endswith(".json") else json.loads(sys.argv[2])
        print(post("/__script", {"steps": steps}))
