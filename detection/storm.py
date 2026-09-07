def compute_threat_score(reflectivity, lightning, wind_speed, rotation):
    score = 0
    # Example logic:
    if reflectivity > 50:
        score += 20
    if lightning > 10:
        score += 15
    if wind_speed > 50:
        score += 10
    if rotation > 0.1:
        score += 15
    return min(score, 100)