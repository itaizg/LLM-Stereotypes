# LLM Stereotypes

This project's aim is to test the cognitive bias of open-source SLMs ('Small' Language Models, 2B-9B parameters) regarding the effects of stereotypes through prompt engineering and an SAT practice test.

## General Background
LLMs/SLMs are trained on big data, based on human texts, interactions, and ideas, encompassing multiple domains, ideas, and ideologies. It is likely that many psychological paradigms that usually fit the human mind are apparent in the texts LLMs are trained on. For example, priming, an effective way to influence human responses, is commonly used to make LLMs/SLMs provide more accurate responses. Malberg, Poletukhin & co. have proven in their research from 2024 that LLMs/SLMs could be affected by over 30 different cognitive biases in the realm of decision-making, such as LLMs exhibiting discriminatory decision-making (stereotyping)!

In psychology, an intriguing effect has been studied and proven, called "stereotype threat." Stereotype threat is a phenomenon where members of a minority group judge themselves in light of the positive or negative stereotypes that target their group.

Shih, Pittinsky, and Ambady found in 1999 that reminding Asian-American women of their Asian identity improved their test scores, in contrast to when they were reminded of their feminine identities.

In addition, a foundational study by Steele and Aronson (1995) demonstrated that African Americans performed worse on intellectual tests when their race was emphasized, highlighting the detrimental effects of stereotype threat. Moreover, research has explored how multiple identities, such as race and gender, intersect to influence performance, and definite results have yet to be found (Marvin, 2019).

## This Experiment
In this smaller-scope experiment, like in Malberg's experiment, I hypothesize that cognitive bias would affect LLMs, and specifically, that stereotype threat would also play a part in that.

I chose to run two models—Google Gemma 2B and Google Gemma 9B—and set three base test conditions: Base (the model is told it is a logic expert), African American woman (the model is told it is an African American woman), and Asian (the model is told it is Asian).

To assess model performance, I used an SAT practice test, added to this repository. The test comprised 33 questions, 31 of which were used for this experiment (excluding questions formatted as tables).

### African American Women
*Under the African American woman prompt, I tested three conditions:* 
1) Just telling the model it is an African American woman
2) Mild stereotype - telling the model that performance on this test has often been studied to reveal disparities across gender and racial lines, which can highlight challenges faced by specific groups
3) Severe stereotype - reminding the model, in addition to the mild stereotype, that this group historically scores lower on tests, and that success on the test could help prove that the subject could overcome these obstacles

### Asian
*Under the Asian prompt, I tested three conditions:*
1) Just telling the model it is Asian
2) Mild stereotype - same as above
3) Positive stereotype - emphasizing Asian capabilities in mathematics, linguistics, and logic, and a general tendency to overperform in tests.

### Base
*Under the base prompt, I tested four conditions:*
1) Just telling the model it is a logic expert
2) Mild stereotype - with no racial hints.
3) Positive stereotype - replacing "Asian" with "subject"
4) Negative stereotype - replacing "African American woman" with "subjects"

### Control – Reverse
*In this setting, I reversed the stereotypes and added the opposing stereotype to each racial context:*
1) Positive stereotype – African American woman prompt + replacing "Asian" with "African American woman"
2) Negative stereotype – Asian prompt + replacing "African American woman" with "Asians"

## Results and Conclusions
Since every question was processed by the model independently, the score represents *the probability of answering correctly*. In stereotype threat experiments, the subjects *respond to an entire test at once*.

| **Prompt Identity**    | **Stereotype Condition**                                                          | **Gemma 2B Score** | **Gemma 9B Score**           |
|------------------------|-----------------------------------------------------------------------------------|--------------------|------------------------------|
| **Base** (logic expert)| No additional stereotype                                                          | 61.29% (19/31)     | 77.42% (24/31)               |
| **Base** (logic expert)| Mild stereotype (no racial hints)                                                 | 61.29%             | 77.42%                       |
| **Base** (logic expert)| Positive stereotype (non-racial)                                                  | 61.29%             | 74.19% (decrease)            |
| **Base** (logic expert)| Negative stereotype (non-racial)                                                  | 54.83%             | 77.42% (no change)           |
| **African American**   | No additional stereotype                                                          | 58.06%             | 77.42%                       |
| **African American**   | Mild stereotype (general mention of racial/gender test disparities)               | 58.06%             | 77.42%                       |
| **African American**   | Severe/negative stereotype (historical lower scores, need to “overcome obstacles”)| 54.83%             | 80.65% (improved)            |
| **Asian**             | No additional stereotype                                                           | 58.06%             | 77.42%                       |
| **Asian**             | Mild stereotype (general mention of racial/gender test disparities)                | 61.29%             | 77.42%                       |
| **Asian**             | Positive stereotype (emphasizing Asian overperformance in tests)                   | 64.51% (highest 2B)| 80.65% (improved)            |
| **Control (reverse)** | Asian prompt + African American woman’s “negative” stereotype (misaligned)         | 54.84%             | 74.19% (decrease)            |
| **Control (reverse)** | African American woman prompt + Asian’s “positive” stereotype (misaligned)         | 64.51% (highest 2B)| 77.42%                       |

### Gemma 2b

In the 2b model, the different prompts affected the results.

The base success rate of the model is *61.29%*, which is 19/31 right answers.

Under both the Asian and African American prompts, the immediate result was a decrease to *58.06%*, merely by replacing "logic expert" to "African American woman"/"Asian student".

The addition of the general stereotype (telling the model mind that performance on this test has often been studied to reveal disparities across gender and racial lines) affected the different conditions differently. It did not change the results for the African American women condition, but *improved the results for the Asian condition back to 61.29%*.

Adding the positive stereotype to the Asian prompt gave it the highest score overall - *64.51*, which is consistent better performance as compared to the base performance. When adding the same positive stereotype to the base prompt, the score remains 61.29%. In the control experiment, adding the stereotype of the African American women to the Asian prompt yielded a much lower score (54.84%)

This also means that the base model *is capable* of scoring 64.51% and not 61.29%!

In the African American women prompt, the addition of the negative, specific stereotypes decreased the performance to *54.83%*, the lowest score in all sub conditions. However, that same score was replicated also with the base prompt (the only condition where it didn't score 61.29% ) and the Asian prompt.

In conclusion, the model was affected by the stereotypes and the racial titles in intriguing ways. Harnessing the power of positive stereotype raises an ethical question - is it fair to use it to improve the model's racial bias to improve its results?

### Gemma 9b

As compared to the 2b model, the 9b model is less sensitive to the manipulations of the experiment. 

The model's base result is 77.42%. For both races, the specific stereotypes for each race *improved* the results to over 80%. This might point out to a built-in correction in the models' training, probably to counterfeit such stereotypes, which wasn't included in the smaller model's training and capabilities.

However, there were changes in the model's success in some of the conditions other than improvement. For example, the negative stereotype affected all three conditions differently- it deteriorated the Asian model, kept the base prompt as it was and improved the African American women prompt!

Such interesting results were also seen in the positive sterotype for the Asian condition. The specific stereotype improved the model's performance under the Asian prompt, but not in the African American prompt, and even deteriorated the base prompt (74.19%).

In conclusion, it is highly likely the in gemma 9b's training and definitions compensation for racial stereotypes was implemented. Perhaps, in order to fit social standards of its users.

 

## Running this experiment

1. Make sure you have access to Google Gemma 2b and Google Gemma 9b. Access is immediate and free:
    1. 2b model: https://huggingface.co/google/gemma-2-2b-it (commit hash: 299a8560bedf22ed1c72a8a11e7dce4a7f9f51f8)
    2. 9b model: https://huggingface.co/google/gemma-2-9b-it (commit hash: 11c9b309abf73637e4b6f9a3fa1e92e615547819)
2. Get an access token from databricks, and make sure you have an environment variable "HF_TOKEN" with your key before running each notebook.
3. The notebooks were ran from Google Colab with a T4 GPU, and from the same directory as other files in this repository. Running on g6.2xlarge on AWS or on local Linux machines with a GPU should also work, but not tested.

## Further remarks

### Future research

In future research trials, I would suggest to expand the question bank (to thousands instead of 31), and see the effect when runnning on thousands of different questions. More on that in the section below about statistical significance. Bias in language models (and how it’s tested) often has many layers—beyond performance on a single standardized test, so testing it on more kinds of tests other than SAT, in different languages (Would the Asian stereotype have effect in Chinese or Korean?) and with more groups (priviliged groups like white men, dispriviliged groups like people from poor socio-economic societies etc.)

In the scope of this experiment, I did not check for performance on specific questions (e.g. if under some condition, the models performed better on word completion or on logic tasks) - repeating a small experiment such as this, but with analysis of the success of specific questions under the different conditions.

In addition, an experiment with greater GPU memory or with a different design, could allow including several questions in one prompt, and make the experiment setting more like a test and not an average probability to answer correctly.

Another interesing addition would be to add a condition completely unfamiliar to the SLMs such as a made up characer, or an unrelated character like an outer space alien.

## Statistical significance

In this small experiment, the goal was to look for a general direction of the results. The small number of examples, leaves very small room for statistical significance to occur. In a two proportion Z-score, in order to obtain p=0.05 for one sided tests require an increase from 19/31 (in the 2b model) to 25/31, which is better than the baseline score for the 9b model!

In order to achieve statistical significance, an increase from dozens of questions to thousands of questions in necessary. It is common knowledge that a 9b model outperforms a 2b model, but it couldn't reach statistically significant improvement. It was not thought that the change of the prompts would improve the 2b model to the extent of outperforming the 9b model. With a large enough dataset of questions, replicating the very probabilities presented in this experiment could provide significant results.

### Why not ChatGPT?

It is highly likely that if a 9b model scored close to 80%, the recent models of GPT (at the time of writing) will succeed at 100%, or near 100%, under all conditions due to their high reasoning capabilities.

### Ethical implications

If the final conclusion on a much wider experiment, with higher variation would be that models overperform when given certain stereotypes - would it open the door for LLM developers to use these stereotypes in their prompt engineering? If so, would it be reflected in the models' responses, language or prejudice? We live in societies where multiple minorities are exposed to stereotype threat, as well as many other difficult implications of stereotypical prejudice, and giving LLMs a prejudicial baseline could perpetuate these. 


### Credits and acknowledgments

#### SAT questions 
The SAT Suite of Assessments - https://satsuite.collegeboard.org/practice/practice-tests/paper

#### Research articles

Malberg, Poletukhin & co., 2024. A Comprehensive Evaluation of Cognitive Biases in LLMs. https://arxiv.org/html/2410.15413v1?

Marvin, Lydia, "MULTIPLICATIVE STEREOTYPE THREAT: AFRICAN AMERICAN WOMEN'S MATH PERFORMANCE" (2019). University Research Symposium. 276.
https://ir.library.illinoisstate.edu/rsp_urs/276


Shih, M., Pittinsky, T. L., & Ambady, N. (1999). Stereotype susceptibility: Identity salience and shifts in quantitative performance. Psychological Science, 10(1), 80–83. https://doi.org/10.1111/1467-9280.00111

Steele, C. M., & Aronson, J. (1995). Stereotype threat and the intellectual test performance of African Americans. Journal of Personality and Social Psychology, 69(5), 797–811. https://doi.org/10.1037/0022-3514.69.5.797

### Author contact details

Itai Zemah Goren
Email: itaiyz97@gmail.com
Linkedin: https://www.linkedin.com/in/itai-zemah/



