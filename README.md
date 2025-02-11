# LLM_stereotypes
## General background
LLMs are trained on big data, based on human texts, interactions and ideas, encompassing multiple domains, ideas and ideologies. It is likely that many psychological paradigms are apparent in the texts LLMs are being trained on. For example, priming is a common way to make LLMs provide more accurate responses. Chen, Liu & co. have proven in their research from 2024 that LLMs could be affected by cognitive biases beyond, such as the anchoring bias, decoy effect, and reference dependence, during relevance assessment tasks.

In Psychology, an intriguing effect had been studies and proven, names "Sterotype threat". Stereotype threat is a phenomenon where members of a minority group, judge themselves in light of the positive or negative stereotypes that target their group. 

Shih, Pittinsky, and Ambady found in 1999 that reminding Asian-American women on their Asian identity improved their tests scores, in contrast to when they were reminded on their feminine identities. 

In addition, A foundational study by Steele and Aronson (1995) demonstrated that African Americans performed worse on intellectual tests when their race was emphasized, highlighting the detrimental effects of stereotype threat. Moreover, research has explored how multiple identities, such as race and gender, intersect to influence performance, and definite results have yet to be found.(Marvin, 2019).

## This experiment
In this smaller scope experiment, like in Liu's experiment, I hypothesize cognitive bias would affect LLMs, and specifically that stereotype threat would also make part of that.

I chose to run two models - Google Gemma 2b and Google Gemma 9b, and I set three base test conditions: Base (the model is told it is an AI expert) African American women (The model is told it is an African American woman), and Asian (The model is told it is Asian). 

ChatGPT was ommitted as 

To assess model performance, I used a SAT practice test, added to this repository. The test comprised of 33 questions, 31 of which were used for this experiment

### African American Women
*Under the African American woman prompt, I tested three conditions:* 
1) Just telling the model it is an African American woman
2) Mild stereotype -  telling the model mind that performance on this test has often been studied to reveal disparities across gender and racial lines, which can highlight challenges faced by specific groups
3) Severe stereotype - reminding the model, in addition to the mild stereotype, that this group historically scores lower on tests, and that success on the test could help prove that the subject could overcome these obstacles

### Asian
*Under the Asian prompt, I tested three conditions:*
1) Just telling the model it is an Asian
2) Mild stereotype - same as above
3) Positive stereotype - emphasizing Asian capabilities in mathematics, linguistics and logic, and a general tendency to overperform in tests.

### Base
*Under the base prompt, I tested four conditions*
1) Just telling the model it is a logic expert
2) Mild stereotype - with no racial hints.
3) Positive stereotype - replacing "Asian" with "subject"
4) Negative stereortype - replaceing "African American women" with "subjects"

## Results

### Gemma 2b
The base score for the model was 61.29%, when running

## Running this experiment