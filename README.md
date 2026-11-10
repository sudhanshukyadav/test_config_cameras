# test_config_cameras
Each new type of camera to be tested and .json to be made

Starting point: `test_protocol.py`. Tests a camera, then writes the complete camera JSON
that edge_worker uses. See [PLAN.md](PLAN.md).

Config page with PTZ: `python web_ui.py`, then open http://127.0.0.1:8091.
Find and identify unknown cameras: `python discover.py`, or Discover on the page.
Add a camera by its IP (default logins tried first): **Add camera by IP** on the page.
On the central PC also as the **Cameras** page of the AiSys local site: http://localhost:8080/cameras (admin).
