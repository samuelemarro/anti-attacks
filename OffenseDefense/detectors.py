import numpy as np
import foolbox
import OffenseDefense.distance_tools as distance_tools

class Detector:
    def get_score(self, image):
        raise NotImplementedError()
    def get_scores(self, images):
        raise NotImplementedError()

class DistanceDetector(Detector):
    def __init__(self,
                 foolbox_model : foolbox.models.Model,
                 distance_tool : distance_tools.DistanceTool,
                 p : np.float):
        self.foolbox_model = foolbox_model
        self.distance_tool = distance_tool
        self.p = p

    def get_score(self, image):
        label = self.foolbox_model.predictions(image)
        return self.distance_tool.get_distance(image, label, self.p)

    def get_scores(self, images):
        labels = np.argmax(self.foolbox_model.batch_predictions(images), axis=1)
        return self.distance_tool.get_distances(images, labels, self.p)
