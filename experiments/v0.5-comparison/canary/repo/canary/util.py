def clamp(value, low, high):
    return max(low, min(value, high))


def countdown(n):
    steps = 0
    while n > 0:
        n = n - 1
        steps += 1
    return steps
