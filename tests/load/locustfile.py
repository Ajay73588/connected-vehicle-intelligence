"""Locust alternative (pip install locust):  locust -f tests/load/locustfile.py --host http://localhost:8000
Users alternate dashboard reads, alert paging, vehicle details and Fleet Copilot questions."""

import random

from locust import HttpUser, between, task


class FleetManager(HttpUser):
    wait_time = between(0.5, 2)

    def on_start(self):
        r = self.client.post("/auth/login", json={"email": "fleet_manager@acme.demo", "password": "Demo@12345"})
        self.h = {"Authorization": "Bearer " + r.json()["access_token"]}
        self.ids = [v["id"] for v in self.client.get("/api/v1/vehicles?limit=50", headers=self.h).json()["items"]]

    @task(5)
    def dashboard(self):
        self.client.get("/api/v1/analytics/summary", headers=self.h, name="summary")

    @task(4)
    def alerts(self):
        self.client.get("/api/v1/alerts?status=open&limit=50", headers=self.h, name="alerts")

    @task(3)
    def vehicle(self):
        self.client.get(f"/api/v1/vehicles/{random.choice(self.ids)}", headers=self.h, name="vehicle")

    @task(1)
    def copilot(self):
        self.client.post("/api/v1/agent/chat", json={"message": "Which vehicles are at highest maintenance risk?"}, headers=self.h, name="copilot")
