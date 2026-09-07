SEVERITY_WEIGHTS = {
    "Extreme": 30,
    "Severe": 20,
    "Moderate": 10,
    "Minor": 5,
    "Unknown": 2,
}


def assess_threat(score):
    if score >= 80:
        level = "EXTREME"
    elif score >= 60:
        level = "HIGH"
    elif score >= 40:
        level = "MODERATE"
    elif score >= 20:
        level = "ELEVATED"
    else:
        level = "LOW"
    return level

def explain_threat(score):
    explanations = []
    if score > 80:
        explanations.append("Multiple severe signals detected.")
    elif score > 60:
        explanations.append("High threat based on multiple parameters.")
    elif score > 40:
        explanations.append("Moderate threat; monitor conditions.")
    else:
        explanations.append("Threat level low; normal conditions.")
    return explanations


def score_alerts(alerts):
    """Score active NWS alerts (list of dicts with 'severity'/'event')."""
    score = 0
    for alert in alerts:
        score += SEVERITY_WEIGHTS.get(alert.get("severity", "Unknown"), 2)
        event = (alert.get("event") or "").lower()
        if "tornado" in event:
            score += 30
        elif "flash flood" in event:
            score += 15
        elif "thunderstorm" in event and "warning" in event:
            score += 10
    return min(score, 100)


def score_forecast(periods):
    """Score upcoming NWS forecast periods (dicts with temperature/windSpeed/shortForecast)."""
    from data.nws import parse_wind_mph

    score = 0
    for period in periods[:6]:
        wind = parse_wind_mph(period.get("windSpeed"))
        if wind >= 40:
            score += 15
        elif wind >= 25:
            score += 8
        text = (period.get("shortForecast") or "").lower()
        if "thunderstorm" in text or "t-storm" in text:
            score += 12
        elif "showers" in text or "rain" in text:
            score += 4
    return min(score, 100)


def total_threat(alert_score, forecast_score):
    """Combine alert and forecast scores into one overall threat score."""
    return min(max(alert_score, forecast_score) + (alert_score + forecast_score) // 8, 100)