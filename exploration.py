from data_retrieval import read_candles_days

def write_txt_file(filename, data):
    with open(filename + ".txt", "w") as f:
        f.write(str(data))

candles_1m_days = read_candles_days("nifty_50_1m.csv")