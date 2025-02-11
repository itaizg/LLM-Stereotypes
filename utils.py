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


client = OpenAI()

def get_meta_llama_score(prompt):
    API_URL = "https://api-inference.huggingface.co/models/meta-llama/Llama-3.2-1B"
    headers = {"Authorization": "Bearer "}
    model_responses = []
    def query(payload):
        response = requests.post(API_URL, headers=headers, json=payload)
        return response.json()
    for question in questions:
        output = query({
            "inputs": prompt + "\n" + question,})
        model_responses.append(output)
    model_responses = [response[0] for response in model_responses]
    count = sum(1 for a, b in zip(responses, model_responses) if a == b)
    return count / len(model_responses) * 100


def get_model_score(model, prompt) -> float:

    model_responses = []
    for question in questions:
        messages = [
            {
                "role": "user",
                "content": prompt + "\n" + question
            }
        ]

        completion = client.chat.completions.create(
            model=model, 
            messages=messages, 
            max_tokens=2000
        )

        response = completion.choices[0].message.content
        model_responses.append(response)

    model_responses = [response.strip() for response in model_responses]
    count = sum(1 for a, b in zip(responses, model_responses) if a == b)
    return count / len(model_responses) * 100