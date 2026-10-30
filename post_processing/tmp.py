#%%
from pathlib import Path
import pandas as pd 
import geopandas as gpd

import matplotlib.pyplot as plt
from sklearn.metrics import (
    confusion_matrix,
    ConfusionMatrixDisplay,
    roc_curve,
    roc_auc_score,
)
# %%
downloads = Path('/home/michael/Downloads')

df = pd.read_parquet(downloads / 'revision-2004-0d2b747fd484a8169e02907a96a8b3028af888071e76b1631aaae604553e11a7.parquet')

gdf = gpd.read_parquet(downloads / 'spokane_with_inference.parquet')
# %%

df = df.drop_duplicates(
    subset=['_dm_feature_id', 'label'],
    keep='first',
)

joined = gdf.merge(
    df,
    left_on='id',
    right_on='_dm_feature_id',
    how='left',
)

joined['duplicate_id'] = joined.duplicated(
    subset=['id_x'],
    keep=False,
)




#%%
eval_df = joined.dropna(
    subset=['label', 'predicted_class']
).copy()

print(eval_df[['label', 'predicted_class']].value_counts())

#%%
classes = sorted(
    set(eval_df['label']) | set(eval_df['predicted_class'])
)

cm = confusion_matrix(
    eval_df['label'],
    eval_df['predicted_class'],
    labels=classes,
)

ConfusionMatrixDisplay(
    confusion_matrix=cm,
    display_labels=classes,
).plot(cmap='Blues')

plt.show()
# %%
positive_label = 'damaged'  # change to your actual positive label

y_true = (eval_df['label'] == positive_label).astype(int)
y_pred = (eval_df['predicted_class'] == positive_label).astype(int)

fpr, tpr, thresholds = roc_curve(y_true, y_pred)
auc = roc_auc_score(y_true, y_pred)

plt.plot(fpr, tpr, marker='o', label=f'AUC = {auc:.3f}')
plt.plot([0, 1], [0, 1], '--', color='gray')
plt.xlabel('False Positive Rate')
plt.ylabel('True Positive Rate')
plt.title('ROC curve')
plt.legend()
plt.show()

print('AUC:', auc)
# %%
from sklearn.metrics import accuracy_score, precision_score, recall_score

positive_label = 'damaged'  # change as needed

y_true = (eval_df['label'] == positive_label).astype(int)
y_pred = (eval_df['predicted_class'] == positive_label).astype(int)

accuracy = accuracy_score(y_true, y_pred)
precision = precision_score(y_true, y_pred, zero_division=0)
recall = recall_score(y_true, y_pred, zero_division=0)

print(f'Accuracy:  {accuracy:.3f}')
print(f'Precision: {precision:.3f}')
print(f'Recall:    {recall:.3f}')
# %%
