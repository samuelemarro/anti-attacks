import logging

import click
import foolbox
import numpy as np
import sklearn
import sys
import torch

import OffenseDefense
import OffenseDefense.attacks as attacks
import OffenseDefense.batch_attack as batch_attack
import OffenseDefense.defenses as defenses
import OffenseDefense.detectors as detectors
import OffenseDefense.distance_tools as distance_tools
import OffenseDefense.loaders as loaders
import OffenseDefense.model_tools as model_tools
import OffenseDefense.parsing as parsing
import OffenseDefense.rejectors as rejectors
import OffenseDefense.tests as tests
import OffenseDefense.training as training
import OffenseDefense.utils as utils

logger = logging.getLogger('OffenseDefense')

# TODO: Test preprocessing options
# TODO: Save models, not weights
# TODO: Allow for optional model weights?
# TODO: Check that the pretrained model does not contain normalisation inside?
# TODO: British vs American spelling
# TODO: Check composite workers
# TODO: load_partial_state_dict and get_torch_model() with optional n+1 classes
# TODO: Finish train_approximator
# TODO: Download the cifar100 weights for densenet-bc-100-12 (when available)
# TODO: Upload both of them and update the links in config.ini
# TODO: counter_attack_workers doesn't do anything!

# IMPORTANT:
# Shallow attacks the standard model, then it is evaluated on the defended model
# Substitute and Black-Box attack the defended model
# This means that you cannot write the sanity check "Shallow is the same as
# a Substitute that uses the original as gradient estimator"

@click.group()
def main(*args):
    logging.basicConfig()
    logging.captureWarnings(True)

    # Print the messages to console
    root = logging.getLogger()
    handler = logging.StreamHandler(sys.stdout)
    handler.formatter = logging.Formatter('%(levelname)s:%(name)s:%(message)s')
    root.addHandler(handler)


@main.command()
@parsing.global_options
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.dataset_options('test')
@parsing.test_options('attack')
@parsing.attack_options(parsing.supported_attacks)
@click.option('--adversarial-dataset-path', type=click.Path(exists=False, file_okay=True, dir_okay=False), default=None,
              help='The path to the .zip file where the adversarial samples will be saved. If unspecified, no adversarial samples will be saved.')
@click.option('--no-test-warning', is_flag=True,
              help='Disables the warning for running this test on the test set.')
def attack(options, adversarial_dataset_path, no_test_warning):
    """
    Runs an attack against the model.

    \b
    Stores the following results:
        Success Rate: The success rate of the attack.
        Average Distance: The average L_p distance of the successful adversarial samples from their original samples.
        Median Distance: The median L_p distance of the successful adversarial samples from their original samples.
        Adjusted Median Distance: The median L_p distance of the adversarial samples from their original samples, treating failed attacks as samples with distance Infinity.
    """

    attack_p = options['attack_p']
    attack_parallelization = options['attack_parallelization']
    attack_name = options['attack_name']
    attack_workers = options['attack_workers']
    command = options['command']
    dataset_type = options['dataset_type']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    results_path = options['results_path']
    torch_model = options['torch_model']

    criterion = foolbox.criteria.Misclassification()

    if attack_parallelization:
        batch_worker = batch_attack.TorchModelWorker(torch_model)
    else:
        batch_worker = None

    attack = parsing.parse_attack(
        attack_name, attack_p, foolbox_model, criterion)

    save_adversarials = adversarial_dataset_path is not None

    if dataset_type == 'test' and save_adversarials and not no_test_warning:
        logger.warning('Remember to use \'--dataset-type train\' if you plan to use the generated adversarials '
                       'to train or calibrate an adversarial detector. You can disable this warning by passing '
                       '\'--no-test-warning\'.')

    samples_count, correct_count, successful_attack_count, distances, adversarials, adversarial_ground_truths = tests.attack_test(foolbox_model, loader, attack, attack_p,
                                                                                                                                  batch_worker, attack_workers, save_adversarials=save_adversarials)

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    failure_count = correct_count - successful_attack_count
    average_distance, median_distance, adjusted_median_distance = utils.distance_statistics(
        distances, failure_count)

    info = [['Base Accuracy', '{:2.2f}%'.format(accuracy * 100.0)],
            ['Success Rate', '{:2.2f}%'.format(success_rate * 100.0)],
            ['Average Distance', '{:2.2e}'.format(average_distance)],
            ['Median Distance', '{:2.2e}'.format(median_distance)],
            ['Adjusted Median Distance', '{:2.2e}'.format(
                adjusted_median_distance)],
            ['Samples Count', str(samples_count)],
            ['Correct Count', str(correct_count)],
            ['Successful Attack Count', str(successful_attack_count)]]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)

    if save_adversarials:
        dataset = list(
            zip(adversarials, adversarial_ground_truths)), success_rate
        utils.save_zip(dataset, adversarial_dataset_path)


@main.command()
@parsing.global_options
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.dataset_options('test', 'test')
@parsing.test_options('accuracy')
@click.option('--top-ks', nargs=2, type=click.Tuple([int, int]), default=(1, 5), show_default=True,
              help='The two top-k accuracies that will be computed.')
def accuracy(options, top_ks):
    """
    Computes the accuracy of the model.

    \b
    Stores the following results:
        Top-K Accuracies: The accuracies, where k values are configurable with --top-ks.
    """

    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    results_path = options['results_path']

    accuracies = tests.accuracy_test(
        foolbox_model, loader, top_ks)

    info = [['Top-{} Accuracy:'.format(top_k), '{:2.2f}%'.format(accuracy * 100.0)]
            for top_k, accuracy in zip(top_ks, accuracies)]
    utils.save_results(results_path, command=command, info=info)


@main.command()
@parsing.global_options
@parsing.dataset_options('test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('detector-roc')
@parsing.distance_tool_options
@parsing.counter_attack_options(False)
@parsing.detector_options
@parsing.adversarial_dataset_options
@click.option('--score_dataset_path', type=click.Path(exists=False, file_okay=True, dir_okay=False), default=None,
              help='The path to the .zip file where the scores will be saved with their corresponding images. If unspecified, no scores will be saved.')
@click.option('--no-test-warning', is_flag=True,
              help='Disables the warning for running this test on the test set.')
def detector_roc(options, score_dataset_path, no_test_warning):
    """
    Uses a detector to identify adversarial samples and computes the ROC curve.
    """

    adversarial_loader = options['adversarial_loader']
    command = options['command']
    dataset_type = options['dataset_type']
    detector = options['detector']
    failure_value = options['failure_value']
    foolbox_model = options['foolbox_model']
    genuine_loader = options['loader']
    results_path = options['results_path']

    save_scores = score_dataset_path is not None

    if dataset_type == 'test' and not no_test_warning:
        logger.warning('Remember to use \'--dataset-type train\' if you plan to use the results '
                       'to pick a threshold for other tests. You can disable this warning by passing '
                       '\'--no-test-warning\'.')

    genuine_scores, adversarial_scores, genuine_samples, adversarial_samples = tests.roc_curve_test(
        foolbox_model, genuine_loader, adversarial_loader, detector, save_scores)

    false_positive_rates, true_positive_rates, thresholds = utils.roc_curve(
        genuine_scores, adversarial_scores)

    best_threshold, best_tpr, best_fpr = utils.get_best_threshold(
        true_positive_rates, false_positive_rates, thresholds)
    area_under_curve = sklearn.metrics.auc(
        false_positive_rates, true_positive_rates)

    info = [['ROC AUC', '{:2.2f}%'.format(area_under_curve * 100.0)],
            ['Best Threshold', '{:2.2e}'.format(best_threshold)],
            ['Best True Positive Rate', '{:2.2f}%'.format(best_tpr * 100.0)],
            ['Best False Positive Rate', '{:2.2f}%'.format(best_fpr * 100.0)]]

    header = ['Genuine Scores', 'Adversarial Scores',
              'Thresholds', 'True Positive Rates', 'False Positive Rates']

    true_positive_rates = ['{:2.2}%'.format(
        true_positive_rate) for true_positive_rate in true_positive_rates]
    false_positive_rates = ['{:2.2}%'.format(
        false_positive_rate) for false_positive_rate in false_positive_rates]

    columns = [genuine_scores, adversarial_scores,
               thresholds, true_positive_rates, false_positive_rates]

    utils.save_results(results_path, table=columns, command=command,
                       info=info, header=header)

    if save_scores:
        # Remove failures

        genuine_not_failed = np.not_equal(genuine_scores, failure_value)
        genuine_samples = genuine_samples[genuine_not_failed]
        genuine_scores = genuine_scores[genuine_not_failed]

        adversarial_not_failed = np.not_equal(
            adversarial_scores, failure_value)
        adversarial_samples = adversarial_samples[adversarial_not_failed]
        adversarial_scores = adversarial_scores[adversarial_not_failed]

        genuine_list = zip(genuine_samples, genuine_scores)
        adversarial_list = zip(adversarial_samples, adversarial_scores)

        dataset = (genuine_list, adversarial_list)

        utils.save_zip(dataset, score_dataset_path)


@main.group()
def defense():
    pass


@defense.group(name='rejector')
def rejector_defense():
    pass


@rejector_defense.command(name='shallow')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/rejector/shallow')
@parsing.attack_options(parsing.supported_attacks)
@parsing.distance_tool_options
@parsing.counter_attack_options(False)
@parsing.detector_options
@parsing.rejector_options
def shallow_rejector(options):
    attack_name = options['attack_name']
    attack_parallelization = options['attack_parallelization']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    attack_p = options['attack_p']
    rejector = options['rejector']
    results_path = options['results_path']
    torch_model = options['torch_model']

    if attack_parallelization:
        batch_worker = batch_attack.TorchModelWorker(torch_model)
    else:
        batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the undefended model
    attack = parsing.parse_attack(
        attack_name, attack_p, foolbox_model, criterion)

    samples_count, correct_count, successful_attack_count, distances = tests.shallow_rejector_test(
        foolbox_model, loader, attack, attack_p, rejector, batch_worker, attack_workers)

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)


@rejector_defense.command(name='black-box')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/rejector/black-box')
@parsing.attack_options(parsing.black_box_attacks)
@parsing.distance_tool_options
@parsing.counter_attack_options(False)
@parsing.detector_options
@parsing.rejector_options
def black_box_rejector(options):
    attack_name = options['attack_name']
    attack_p = options['attack_p']
    attack_parallelization = options['attack_parallelization']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    rejector = options['rejector']
    results_path = options['results_path']

    # The defended_model returns [y1, y2 ... yN, -inf] if it believes
    # that the sample is valid, otherwise it returns [0, 0 ... 0, 1]
    # This means that if the top label is the last one, it was classified as adversarial.
    # On a genuine dataset, this should never happen (if the rejector is perfect).

    defended_model = rejectors.RejectorModel(
        foolbox_model, rejector)

    # detectors.Undetected() adds the condition that the top label must not be the last
    # Note: foolbox.Criterion and foolbox.Criterion should give a combined criterion, but
    # apparently it doesn't work. The documentation recommends using "&"

    criterion = foolbox.criteria.CombinedCriteria(
        foolbox.criteria.Misclassification(), rejectors.Unrejected())

    if attack_parallelization:
        defended_batch_worker = batch_attack.FoolboxModelWorker(defended_model)
    else:
        defended_batch_worker = None

    # The attack will be against the defended model
    attack = parsing.parse_attack(
        attack_name, attack_p, defended_model, criterion)

    samples_count, correct_count, successful_attack_count, distances, _, _ = tests.attack_test(
        defended_model, loader, attack, attack_p, defended_batch_worker, attack_workers, name='Black-Box Rejector Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)


@defense.group(name='model')
def model_defense():
    pass


@model_defense.command(name='shallow')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/model/shallow')
@parsing.custom_model_options
@parsing.attack_options(parsing.supported_attacks)
def shallow_model(options):
    attack_name = options['attack_name']
    attack_parallelization = options['attack_parallelization']
    attack_workers = options['attack_workers']
    command = options['command']
    custom_foolbox_model = options['custom_foolbox_model']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    attack_p = options['attack_p']
    results_path = options['results_path']
    torch_model = options['torch_model']

    if attack_parallelization:
        standard_batch_worker = batch_attack.TorchModelWorker(torch_model)
    else:
        standard_batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the undefended model
    attack = parsing.parse_attack(
        attack_name, attack_p, foolbox_model, criterion)

    samples_count, correct_count, successful_attack_count, distances = tests.transfer_test(
        foolbox_model, loader, attack, attack_p, custom_foolbox_model, standard_batch_worker, attack_workers, name='Shallow Model Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)


@model_defense.command(name='black-box')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.test_options('defense/model/black-box')
@parsing.custom_model_options
@parsing.attack_options(parsing.black_box_attacks)
def black_box_model(options):
    attack_name = options['attack_name']
    attack_parallelization = options['attack_parallelization']
    attack_workers = options['attack_workers']
    command = options['command']
    custom_foolbox_model = options['custom_foolbox_model']
    loader = options['loader']
    attack_p = options['attack_p']
    results_path = options['results_path']
    custom_torch_model = options['custom_torch_model']

    if attack_parallelization:
        custom_batch_worker = batch_attack.TorchModelWorker(custom_torch_model)
    else:
        custom_batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the defended (custom) model
    attack = parsing.parse_attack(
        attack_name, attack_p, custom_foolbox_model, criterion)

    samples_count, correct_count, successful_attack_count, distances, _, _ = tests.attack_test(
        custom_foolbox_model, loader, attack, attack_p, custom_batch_worker, attack_workers, name='Black-Box Model Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)


@defense.group(name='preprocessor')
def preprocessor_defense():
    pass


@preprocessor_defense.command(name='shallow')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/preprocessor/shallow')
@parsing.preprocessor_options
@parsing.attack_options(parsing.supported_attacks)
def shallow_preprocessor(options):
    attack_p = options['attack_p']
    attack_parallelization = options['attack_parallelization']
    attack_name = options['attack_name']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    results_path = options['results_path']
    preprocessor = options['preprocessor']
    torch_model = options['torch_model']

    if attack_parallelization:
        standard_batch_worker = batch_attack.TorchModelWorker(torch_model)
    else:
        standard_batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the undefended model
    attack = parsing.parse_attack(
        attack_name, attack_p, foolbox_model, criterion)

    defended_model = defenses.PreprocessorDefenseModel(
        foolbox_model, preprocessor)

    samples_count, correct_count, successful_attack_count, distances = tests.transfer_test(foolbox_model, loader, attack, attack_p,
                                                                                                defended_model, standard_batch_worker,
                                                                                                attack_workers, name='Shallow Preprocessor Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)

@preprocessor_defense.command(name='substitute')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/preprocessor/substitute')
@parsing.preprocessor_options
@parsing.attack_options(parsing.differentiable_attacks)
@parsing.substitute_options
def substitute_preprocessor(options):
    attack_p = options['attack_p']
    attack_parallelization = options['attack_parallelization']
    attack_name = options['attack_name']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    results_path = options['results_path']
    preprocessor = options['preprocessor']
    substitute_foolbox_model = options['substitute_foolbox_model']
    substitute_torch_model = options['substitute_torch_model']

    defended_model = defenses.PreprocessorDefenseModel(
        foolbox_model, preprocessor)

    composite_model = foolbox.models.CompositeModel(defended_model, substitute_foolbox_model)

    if attack_parallelization:
        defended_batch_worker = batch_attack.CompositeModelWorker(defended_model, substitute_torch_model)
    else:
        defended_batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the defended model with estimated gradients
    attack = parsing.parse_attack(
        attack_name, attack_p, composite_model, criterion)

    samples_count, correct_count, successful_attack_count, distances, _, _ = tests.attack_test(composite_model, loader, attack, attack_p,
                                                                                               defended_batch_worker,
                                                                                               attack_workers, name='Substitute Preprocessor Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)

@preprocessor_defense.command(name='black-box')
@parsing.global_options
@parsing.dataset_options('test', 'test')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.test_options('defense/preprocessor/black-box')
@parsing.preprocessor_options
@parsing.attack_options(parsing.black_box_attacks)
def black_box_preprocessor(options):
    attack_parallelization = options['attack_parallelization']
    attack_name = options['attack_name']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    loader = options['loader']
    results_path = options['results_path']
    attack_p = options['attack_p']
    preprocessor = options['preprocessor']

    defended_model = defenses.PreprocessorDefenseModel(
        foolbox_model, preprocessor)

    if attack_parallelization:
        defended_batch_worker = batch_attack.FoolboxModelWorker(defended_model)
    else:
        defended_batch_worker = None

    criterion = foolbox.criteria.Misclassification()

    # The attack will be against the defended model
    attack = parsing.parse_attack(
        attack_name, attack_p, defended_model, criterion)

    samples_count, correct_count, successful_attack_count, distances, _, _ = tests.attack_test(defended_model, loader, attack, attack_p,
                                                                                               defended_batch_worker,
                                                                                               attack_workers, name='Black-Box Preprocessor Attack')

    accuracy = correct_count / samples_count
    success_rate = successful_attack_count / correct_count

    info = [
        ['Base Accuracy', '{:2.2f}%'.format(
            accuracy * 100.0)],
        ['Base Attack Success Rate', '{:2.2f}%'.format(
            success_rate * 100.0)],
        ['Samples Count', str(samples_count)],
        ['Correct Count', str(correct_count)],
        ['Successful Attack Count', str(successful_attack_count)]
    ]

    header = ['Distances']

    utils.save_results(results_path, table=[distances], command=command,
                       info=info, header=header)


@main.command()
@parsing.global_options
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.dataset_options('test')
@parsing.test_options('parallelization')
@parsing.attack_options(parsing.parallelizable_attacks, mandatory_parallelization=True)
def parallelization(options):
    """
    Compares parallelized attacks with standard ones.
    """

    attack_name = options['attack_name']
    attack_workers = options['attack_workers']
    command = options['command']
    foolbox_model = options['foolbox_model']
    attack_p = options['attack_p']
    results_path = options['results_path']
    loader = options['loader']
    torch_model = options['torch_model']

    batch_worker = batch_attack.TorchModelWorker(torch_model)

    criterion = foolbox.criteria.Misclassification()

    attack = parsing.parse_attack(
        attack_name, attack_p, foolbox_model, criterion)

    samples_count, correct_count, standard_attack_count, parallel_attack_count, standard_distances, parallel_distances = tests.parallelization_test(
        foolbox_model, loader, attack, attack_p, batch_worker, attack_workers)

    standard_failure_count = correct_count - standard_attack_count
    parallel_failure_count = correct_count - parallel_attack_count

    standard_average_distance, standard_median_distance, standard_adjusted_median_distance = utils.distance_statistics(
        standard_distances, standard_failure_count)
    parallel_average_distance, parallel_median_distance, parallel_adjusted_median_distance = utils.distance_statistics(
        parallel_distances, parallel_failure_count)

    standard_success_rate = standard_attack_count / correct_count
    parallel_success_rate = parallel_attack_count / correct_count

    average_distance_difference = (
        parallel_average_distance - standard_average_distance) / standard_average_distance
    median_distance_difference = (
        parallel_median_distance - standard_median_distance) / standard_median_distance
    success_rate_difference = (
        parallel_success_rate - standard_success_rate) / standard_success_rate
    adjusted_median_distance_difference = (
        parallel_adjusted_median_distance - standard_adjusted_median_distance) / standard_adjusted_median_distance

    info = [['Average Distance Relative Difference', average_distance_difference],
            ['Median Distance Relative Difference', median_distance_difference],
            ['Success Rate Difference', success_rate_difference],
            ['Adjusted Median Distance Difference',
                adjusted_median_distance_difference],
            ['Samples Count', str(samples_count)],
            ['Correct Count', str(correct_count)],
            ['Standard Attack Count', str(standard_attack_count)],
            ['Parallel Attack Count', str(parallel_attack_count)]]

    header = ['Standard Distances', 'Parallel Distances']

    utils.save_results(results_path, table=[standard_distances, parallel_distances], command=command,
                       info=info, header=header)


@main.command()
@parsing.global_options
@parsing.dataset_options('train', 'train')
@parsing.train_options
@click.option('--trained-model-path', type=click.Path(file_okay=True, dir_okay=False), default=None,
              help='The path to the file where the model will be saved. If unspecified, it defaults to \'./train_model/$dataset$ $start_time$.pth.tar\'')
def train_model(options, trained_model_path):
    cuda = options['cuda']
    dataset = options['dataset']
    epochs = options['epochs']
    loader = options['loader']
    optimiser_name = options['optimiser_name']
    start_time = options['start_time']

    if trained_model_path is None:
        trained_model_path = parsing.get_training_default_path(
            'train_model', dataset, start_time)

    torch_model = parsing._get_torch_model(dataset)
    torch_model.train()

    if cuda:
        torch_model.cuda()

    optimiser = parsing.build_optimiser(optimiser_name, torch_model.parameters(), options)

    loss = torch.nn.CrossEntropyLoss()

    training.train_torch(torch_model, loader, loss,
                         optimiser, epochs, cuda, classification=True)

    torch.save(torch_model, trained_model_path)

@main.group(name='approximation-dataset')
def approximation_dataset(*args, **kwargs):
    pass

@approximation_dataset.command(name='preprocessor')
@parsing.global_options
@parsing.dataset_options('train', 'train')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.preprocessor_options
@parsing.adversarial_dataset_options
@parsing.approximation_dataset_options('preprocessor')
def approximation_dataset_preprocessor(options):
    adversarial_loader = options['adversarial_loader']
    approximation_dataset_path = options['approximation_dataset_path']
    foolbox_model = options['foolbox_model']
    genuine_loader = options['loader']
    preprocessor = options['preprocessor']

    defended_model = defenses.PreprocessorDefenseModel(
        foolbox_model, preprocessor)


    genuine_approximation_dataset = training.generate_approximation_dataset(defended_model, genuine_loader, 'Genuine Approximation Dataset')
    adversarial_approximation_dataset = training.generate_approximation_dataset(defended_model, adversarial_loader, 'Adversarial Approximation Dataset')

    approximation_dataset = genuine_approximation_dataset + adversarial_approximation_dataset

    utils.save_zip(approximation_dataset, approximation_dataset_path)

@approximation_dataset.command(name='model')
@parsing.global_options
@parsing.dataset_options('train', 'train')
@parsing.standard_model_options
@parsing.custom_model_options
@parsing.adversarial_dataset_options
@parsing.approximation_dataset_options('model')
def approximation_dataset_model(options):
    adversarial_loader = options['adversarial_loader']
    approximation_dataset_path = options['approximation_dataset_path']
    custom_foolbox_model = options['custom_foolbox_model']
    genuine_loader = options['loader']

    genuine_approximation_dataset = training.generate_approximation_dataset(custom_foolbox_model, genuine_loader, 'Genuine Approximation Dataset')
    adversarial_approximation_dataset = training.generate_approximation_dataset(custom_foolbox_model, adversarial_loader, 'Adversarial Approximation Dataset')

    approximation_dataset = genuine_approximation_dataset + adversarial_approximation_dataset

    utils.save_zip(approximation_dataset, approximation_dataset_path)

@approximation_dataset.command(name='rejector')
@parsing.global_options
@parsing.dataset_options('train', 'train')
@parsing.standard_model_options
@parsing.pretrained_model_options
@parsing.attack_options(parsing.supported_attacks)
@parsing.distance_tool_options
@parsing.counter_attack_options(False)
@parsing.detector_options
@parsing.rejector_options
@parsing.adversarial_dataset_options
@parsing.approximation_dataset_options('rejector')
def approximation_dataset_rejector(options):
    adversarial_loader = options['adversarial_loader']
    approximation_dataset_path = options['approximation_dataset_path']
    foolbox_model = options['foolbox_model']
    genuine_loader = options['loader']
    rejector = options['rejector']

    defended_model = rejectors.RejectorModel(foolbox_model, rejector)

    genuine_approximation_dataset = training.generate_approximation_dataset(defended_model, genuine_loader, 'Genuine Approximation Dataset')
    adversarial_approximation_dataset = training.generate_approximation_dataset(defended_model, adversarial_loader, 'Adversarial Approximation Dataset')

    approximation_dataset = genuine_approximation_dataset + adversarial_approximation_dataset

    utils.save_zip(approximation_dataset, approximation_dataset_path)

@main.command()
@parsing.global_options
@parsing.train_options
@parsing.standard_model_options
@click.argument('approximation_dataset_path', type=click.Path(exists=True, file_okay=True, dir_okay=False))
@click.option('--trained-approximator-path', type=click.Path(exists=False, file_okay=True, dir_okay=False), default=None,
              help='The path to the file where the approximator will be saved. If unspecified, it defaults to \'./trained_models/train_approximator/$dataset$ $start_time$.pth.tar\'')
def train_approximator(options, target_model_path, trained_approximator_path):
    base_model = options['base_model']
    cuda = options['cuda']
    dataset = options['dataset']
    epochs = options['epochs']
    loader = options['loader']
    optimiser_name = options['optimiser']
    start_time = options['start_time']

    if trained_approximator_path is None:
        trained_approximator_path = parsing.get_training_default_path(
            'train_approximator', dataset, start_time)

    # Build the pretrained model and the final model (which might be n+1)
    # Transfer some layers (which? how?)
    # Train the remaining layers of the final model
    # Save the model


    training.train_torch(torch_model, loader, torch.nn.CrossEntropyLoss(),
                         optimiser, epochs, cuda, classification=True)

    torch.save(torch_model, trained_approximator_path)


if __name__ == '__main__':
    main()
