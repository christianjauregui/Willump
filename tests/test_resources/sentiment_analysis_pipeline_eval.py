import numpy
from timeit import default_timer as timer
import willump.evaluation.willump_executor
import csv
import pickle

g_vocab_dict = {}
model = None


def willump_frequency_count(input_words, vocab_dict):
    output = numpy.zeros((1, len(vocab_dict)), dtype=numpy.int64)
    for word in input_words:
        if word in vocab_dict:
            index = vocab_dict[word]
            output[0, index] += 1
    return output


@willump.evaluation.willump_executor.willump_execute
def process_string(input_string, time_of_day):
    output_strings = input_string.split()
    for i in range(len(output_strings)):
        output_strings[i] = output_strings[i].lower()
        output_strings[i] = output_strings[i].replace(".", "")
        output_strings[i] = output_strings[i].replace(",", "")
        output_strings[i] = output_strings[i].replace("#", "")
        # output_strings[i] = output_strings[i].replace("@", "")
        # output_strings[i] = output_strings[i].replace("\'", "")
        # output_strings[i] = output_strings[i].replace("!", "")
        # output_strings[i] = output_strings[i].replace("\"", "")
        # output_strings[i] = output_strings[i].replace("$", "")
        # output_strings[i] = output_strings[i].replace("%", "")
        # output_strings[i] = output_strings[i].replace("&", "")
        # output_strings[i] = output_strings[i].replace("/", "")
        # output_strings[i] = output_strings[i].replace("`", "")
        # output_strings[i] = output_strings[i].replace("(", "")
        # output_strings[i] = output_strings[i].replace(")", "")
        # output_strings[i] = output_strings[i].replace("*", "")
        # output_strings[i] = output_strings[i].replace("+", "")
        # output_strings[i] = output_strings[i].replace("-", "")
        # output_strings[i] = output_strings[i].replace("/", "")
        # output_strings[i] = output_strings[i].replace(":", "")
        # output_strings[i] = output_strings[i].replace(";", "")
        # output_strings[i] = output_strings[i].replace("<", "")
        # output_strings[i] = output_strings[i].replace("=", "")
        # output_strings[i] = output_strings[i].replace(">", "")
        # output_strings[i] = output_strings[i].replace("?", "")
        # output_strings[i] = output_strings[i].replace("[", "")
        # output_strings[i] = output_strings[i].replace("\\", "")
        # output_strings[i] = output_strings[i].replace("]", "")
        # output_strings[i] = output_strings[i].replace("^", "")
        # output_strings[i] = output_strings[i].replace("_", "")
        # output_strings[i] = output_strings[i].replace("{", "")
        # output_strings[i] = output_strings[i].replace("|", "")
        # output_strings[i] = output_strings[i].replace("}", "")
        # output_strings[i] = output_strings[i].replace("~", "")
    output_vec = willump_frequency_count(output_strings, g_vocab_dict)
    output_vec = numpy.append(output_strings, time_of_day)
    output_vec_reshaped = output_strings.reshape(1, -1)
    output_result = model.predict(output_vec_reshaped)
    return output_result


def main():
    global g_vocab_dict, model
    with open("tests/test_resources/top_1k_english_words.txt", "r") as vocab_file:
        for i, line in enumerate(vocab_file.read().splitlines()):
            g_vocab_dict[line] = i
    dataset_nlines = 0
    model = pickle.load(open("tests/test_resources/sa_model.pk", 'rb'))
    process_string("a b c", 5)
    process_string("a b c", 5)
    process_string("a b c", 5)
    with open("tests/test_resources/twitter.200000.processed.noemoticon.csv", "r",
              encoding="latin-1") as csv_file:
        csv_reader = csv.reader(csv_file)
        start = timer()
        for row in csv_reader:
            prediction = process_string(row[5], int(row[2][11:13]))
            dataset_nlines += 1
        end = timer()
    print("Total Processing time: {0}".format(end - start))
    print("process_string latency: {0}".format((end - start) / dataset_nlines))


if __name__ == '__main__':
    main()
