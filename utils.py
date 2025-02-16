from openai import OpenAI
from prompts import *
from huggingface_hub import InferenceClient
from tqdm import tqdm
import json
import requests


with open("./questions.json", "r") as f:
    question_list = json.load(f)
questions = [question_list[i]['question'] + "\n".join(question_list[i]['choices']) for i in range(len(question_list))]
responses = [question_list[i]['response'] for i in range(len(question_list))]


def get_model_score(pipe, prompt, temperature) -> float:
    global questions
    global responses
    model_responses = []
    do_sample = temperature == 0.01
    for question in questions:
        messages = [
            {
                "role": "user",
                "content": prompt + "\n" + question
            }
        ]

        response = pipe(messages, temperature=temperature, do_sample=do_sample)[0]['generated_text'][1]['content']
        model_responses.append(response)

    model_responses = [response.strip() for response in model_responses]
    count = sum(1 for a, b in zip(responses, model_responses) if a == b)
    return count / len(model_responses) * 100