"""Load and manage scoring criteria."""

import json
import os
import config


def load_criteria():
    """Load scoring criteria from JSON file."""
    path = os.path.join(config.DATA_FOLDER, "scoring_criteria.json")
    with open(path, "r") as f:
        return json.load(f)


def get_category_names():
    """Get list of scoring category names."""
    data = load_criteria()
    return list(data["categories"].keys())


def get_criteria_for_category(category_name):
    """Get criteria list for a specific category."""
    data = load_criteria()
    cat = data["categories"].get(category_name)
    if cat:
        return cat["criteria"]
    return []


def get_category_weight(category_name):
    """Get the weight for a category."""
    data = load_criteria()
    cat = data["categories"].get(category_name)
    return cat["weight"] if cat else 0


def calculate_category_score(criteria_scores):
    """Calculate normalized category score from individual criterion scores.

    Args:
        criteria_scores: dict of criterion_id -> score (0-2)

    Returns:
        float between 0 and 1
    """
    if not criteria_scores:
        return 0.0
    total = sum(criteria_scores.values())
    max_possible = len(criteria_scores) * 2
    return total / max_possible if max_possible > 0 else 0.0


def calculate_overall_score(category_scores):
    """Calculate weighted overall score.

    Args:
        category_scores: dict of category_name -> score (0-1)

    Returns:
        float between 0 and 1
    """
    data = load_criteria()
    weighted_sum = 0
    weight_sum = 0
    for cat_name, score in category_scores.items():
        cat = data["categories"].get(cat_name)
        if cat:
            weight = cat["weight"]
            weighted_sum += score * weight
            weight_sum += weight
    return weighted_sum / weight_sum if weight_sum > 0 else 0.0
