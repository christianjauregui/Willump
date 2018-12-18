from sklearn.feature_extraction.text import CountVectorizer
import time
import pandas as pd


def vectorizer_transform(input_vect, input_df):
    transformed_result = input_vect.transform(input_df["title"])
    return transformed_result


df = pd.read_csv("tests/test_resources/lazada_data_train.csv", header=None,
                 names=['country', 'sku_id', 'title', 'category_lvl_1', 'category_lvl_2', 'category_lvl_3',
                        'short_description', 'price', 'product_type'])

vect = CountVectorizer(analyzer='char', ngram_range=(2, 6), min_df=0.005, max_df=1.0,
                       lowercase=True, stop_words=None, binary=False, decode_error='replace')
vect.fit(df["title"].tolist())
print("Vocabulary has length %d" % len(vect.vocabulary_))

set_size = len(df)
t0 = time.time()
X_title = vectorizer_transform(vect, df)
time_elapsed = time.time() - t0
print("Title Processing Time %fs Num Rows %d Throughput %f rows/sec" %
      (time_elapsed, set_size, set_size / time_elapsed))