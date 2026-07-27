from decimal import Decimal
from django.utils import timezone
from billing.models import BillingRecord
from collections import defaultdict

import warnings

def get_forecast_for_user(user):
    """
    DEPRECATED: Backward compatibility wrapper for user-based forecast.
    Use get_forecast_for_project instead.
    """
    warnings.warn(
        "get_forecast_for_user is deprecated. Use get_forecast_for_project instead.",
        DeprecationWarning,
        stacklevel=2
    )
    from accounts.models import Project
    project = Project.objects.filter(organization__memberships__user=user).first()
    return get_forecast_for_project(project)

def get_forecast_for_project(project):
    """
    Calculates on-demand monthly cost forecasts for a project, split by currency.
    Returns a dictionary mapping currency string to forecast result dictionary.
    """
    if not project:
        return {}
    # 1. Retrieve all billing records scoped to project with non-null usage_start
    records = BillingRecord.objects.filter(upload__project=project).exclude(usage_start__isnull=True)
    
    # 2. Get today's local date from project timezone configuration
    today = timezone.localdate()
    current_year = today.year
    current_month = today.month
    current_month_str = f"{current_year:04d}-{current_month:02d}"
    
    # Aggregate costs by year-month (YYYY-MM) and currency
    # currency -> month_str -> cost
    currency_monthly_costs = defaultdict(lambda: defaultdict(Decimal))
    future_records_detected = defaultdict(bool)
    encountered_currencies = set()
    
    for r in records:
        usage_dt = r.usage_start
        if timezone.is_aware(usage_dt):
            usage_dt = timezone.localtime(usage_dt)
        usage_date = usage_dt.date()
        yr = usage_date.year
        mn = usage_date.month
        month_str = f"{yr:04d}-{mn:02d}"
        
        # Normalize blank/null currency to UNKNOWN
        curr = (r.currency or "").strip()
        if not curr:
            curr = "UNKNOWN"
        encountered_currencies.add(curr)
            
        # Classify future-dated records
        is_future = (yr > current_year) or (yr == current_year and mn > current_month)
        if is_future:
            future_records_detected[curr] = True
            continue
            
        currency_monthly_costs[curr][month_str] += r.cost

    results = {}
    
    # Process each currency independently
    # Even if no records exist, we need to handle empty state gracefully
    if not encountered_currencies:
        return {}

    all_currencies = sorted(list(encountered_currencies))

    for curr in all_currencies:
        monthly_data = currency_monthly_costs[curr]
        
        # Separate into Completed Historical, Month-to-Date (MTD), and filter out future
        completed_months_data = {}
        mtd_data = None
        
        for month_str, cost in monthly_data.items():
            yr, mn = map(int, month_str.split('-'))
            is_completed = (yr < current_year) or (yr == current_year and mn < current_month)
            
            if is_completed:
                completed_months_data[month_str] = cost
            elif yr == current_year and mn == current_month:
                mtd_data = {
                    "month": month_str,
                    "cost": cost,
                    "type": "MONTH_TO_DATE"
                }

        # Prepare historical months formatted list for display
        historical_months_list = []
        for m_str in sorted(completed_months_data.keys()):
            historical_months_list.append({
                "month": m_str,
                "cost": completed_months_data[m_str],
                "type": "HISTORICAL"
            })

        # If there are no completed months, we cannot build a trend
        if not completed_months_data:
            results[curr] = {
                "forecast_available": False,
                "currency": curr,
                "reason": "At least 3 months of historical billing data are required to generate a cost forecast.",
                "historical_months": [],
                "current_month_mtd": mtd_data,
                "historical_month_count": 0,
                "historical_span_months": 0,
                "missing_month_count": 0,
                "coverage_ratio": Decimal("0.00"),
                "has_future_records": future_records_detected[curr],
                "forecast_months": [],
                "next_month_forecast": Decimal("0.00"),
                "three_month_forecast": Decimal("0.00"),
                "six_month_forecast": Decimal("0.00"),
                "confidence": "LOW",
            }
            continue

        # Sort completed months chronologically
        sorted_completed_months = sorted(completed_months_data.keys())
        
        # Calculate calendar span metrics
        first_month_str = sorted_completed_months[0]
        last_month_str = sorted_completed_months[-1]
        
        f_yr, f_mn = map(int, first_month_str.split('-'))
        l_yr, l_mn = map(int, last_month_str.split('-'))
        
        min_month_index = f_yr * 12 + f_mn
        max_month_index = l_yr * 12 + l_mn
        
        calendar_span_months = (max_month_index - min_month_index) + 1
        observed_completed_months = len(sorted_completed_months)
        missing_month_count = calendar_span_months - observed_completed_months
        coverage_ratio = Decimal(observed_completed_months) / Decimal(calendar_span_months)
        
        # 3 Completed Months Rule
        if observed_completed_months < 3:
            results[curr] = {
                "forecast_available": False,
                "currency": curr,
                "reason": "At least 3 months of historical billing data are required to generate a cost forecast.",
                "historical_months": historical_months_list,
                "current_month_mtd": mtd_data,
                "historical_month_count": observed_completed_months,
                "historical_span_months": calendar_span_months,
                "missing_month_count": missing_month_count,
                "coverage_ratio": coverage_ratio.quantize(Decimal("0.01")),
                "has_future_records": future_records_detected[curr],
                "forecast_months": [],
                "next_month_forecast": Decimal("0.00"),
                "three_month_forecast": Decimal("0.00"),
                "six_month_forecast": Decimal("0.00"),
                "confidence": "LOW",
            }
            continue

        # Prepare regression variables (X, Y) preserving calendar spacing
        X = []
        Y = []
        for month_str in sorted_completed_months:
            yr, mn = map(int, month_str.split('-'))
            month_index = yr * 12 + mn
            x_val = month_index - min_month_index
            X.append(x_val)
            Y.append(completed_months_data[month_str])

        # Ordinary Least Squares Regression using pure Decimal
        n = len(X)
        dec_n = Decimal(n)
        
        x_mean = sum(Decimal(x) for x in X) / dec_n
        y_mean = sum(Y) / dec_n
        
        num = sum((Decimal(X[i]) - x_mean) * (Y[i] - y_mean) for i in range(n))
        den = sum((Decimal(X[i]) - x_mean) ** 2 for i in range(n))
        
        if den == Decimal("0.00"):
            slope = Decimal("0.00")
            intercept = y_mean
        else:
            slope = num / den
            intercept = y_mean - slope * x_mean

        # Calculate residuals and MAE
        residuals = []
        for i in range(n):
            pred_y = intercept + slope * Decimal(X[i])
            pred_y = max(Decimal("0.00"), pred_y)
            residuals.append(Y[i] - pred_y)
            
        mae = sum(abs(r) for r in residuals) / dec_n
        
        if y_mean > Decimal("0.00"):
            normalized_error = mae / y_mean
        else:
            normalized_error = Decimal("0.00")

        # Determine confidence
        confidence = "LOW"
        if y_mean > Decimal("0.00"):
            if n >= 12 and normalized_error <= Decimal("0.10") and coverage_ratio >= Decimal("0.90"):
                confidence = "HIGH"
            elif n >= 6 and normalized_error <= Decimal("0.20") and coverage_ratio >= Decimal("0.75"):
                confidence = "MEDIUM"

        # Calculate residual RMSE for forecast range
        residual_rmse = None
        if n >= 5:
            sum_sq_residuals = sum(r ** 2 for r in residuals)
            residual_rmse = (sum_sq_residuals / dec_n).sqrt()

        # Forecast future months
        # We start predicting from current_month + 1 (the next calendar month)
        # So we predict: current_month + 1, current_month + 2, ..., current_month + 6
        forecast_months = []
        current_month_index = current_year * 12 + current_month
        
        for step in range(1, 7):
            future_month_index = current_month_index + step
            future_x = future_month_index - min_month_index
            
            # Predict cost
            pred = intercept + slope * Decimal(future_x)
            pred_floored = max(Decimal("0.00"), pred).quantize(Decimal("0.01"))
            
            # Calculate range bounds
            if residual_rmse is not None:
                lower_bound = max(Decimal("0.00"), pred_floored - residual_rmse).quantize(Decimal("0.01"))
                upper_bound = (pred_floored + residual_rmse).quantize(Decimal("0.01"))
            else:
                lower_bound = None
                upper_bound = None
                
            future_yr = (future_month_index - 1) // 12
            future_mn = (future_month_index - 1) % 12 + 1
            future_month_str = f"{future_yr:04d}-{future_mn:02d}"
            
            forecast_months.append({
                "month": future_month_str,
                "predicted_cost": pred_floored,
                "lower_bound": lower_bound,
                "upper_bound": upper_bound,
                "type": "FORECAST"
            })

        # Calculate horizon totals from floored future predictions
        next_month_forecast = forecast_months[0]["predicted_cost"]
        three_month_forecast = sum(m["predicted_cost"] for m in forecast_months[:3])
        six_month_forecast = sum(m["predicted_cost"] for m in forecast_months[:6])

        # Prepare historical months formatted list for display
        historical_months_list = []
        for m_str in sorted_completed_months:
            historical_months_list.append({
                "month": m_str,
                "cost": completed_months_data[m_str],
                "type": "HISTORICAL"
            })

        results[curr] = {
            "forecast_available": True,
            "currency": curr,
            "reason": "",
            "historical_months": historical_months_list,
            "current_month_mtd": mtd_data,
            "forecast_months": forecast_months,
            "next_month_forecast": next_month_forecast,
            "three_month_forecast": three_month_forecast,
            "six_month_forecast": six_month_forecast,
            "confidence": confidence,
            "historical_month_count": n,
            "historical_span_months": calendar_span_months,
            "missing_month_count": missing_month_count,
            "coverage_ratio": coverage_ratio.quantize(Decimal("0.0001")),
            "has_future_records": future_records_detected[curr],
            "model": "LINEAR_TREND",
            "limitations": [
                "Forecasts are based on historical OCI billing patterns.",
                "Forecasts cannot know future resource provisioning/deletion.",
                "Forecasts do not know future OCI pricing changes.",
                "Forecasts do not use CPU/RAM/network telemetry.",
                "Unexpected workloads may materially change actual cost.",
                "Forecast values are estimates and not guaranteed bills."
            ]
        }

    return results
