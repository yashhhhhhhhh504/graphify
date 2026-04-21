
def live_test_alpha():
    pass

def live_test_beta():
    live_test_alpha()

class LiveTestGamma:
    def run(self):
        live_test_alpha()
        live_test_beta()
