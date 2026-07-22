from django.db import models


class Simulation(models.Model):
    title = models.CharField(max_length=200)
    current_bill = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    simulated_bill = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    savings = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
